#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""budget_report.py —— 分析会话 token 消耗与工具调用画像，输出可执行建议。

为什么需要
----------
"这次怎么这么费 token"如果没有数字支撑，就只能靠感觉，下次还会犯。
本脚本把一次会话的工具调用记录聚合成画像，并给出**每条建议对应一个具体改法**。

输入
----
支持两种格式（自动识别）：

1. **JSONL** —— 每行一个 JSON 对象。Cursor 的 transcript 形态为
   ``{"role":"assistant","message":{"content":[{"type":"tool_use","name":"Read","input":{...}}]}}``；
   也兼容裸的 ``{"role":..,"content":[...]}``。
2. **JSON** —— 顶层是数组，或 ``{"messages": [...]}``。

用法
----
::

    python budget_report.py --input <会话记录路径> --out report.md
    python budget_report.py --input log.jsonl --top 15 --dump-threshold 20000

输出
----
Markdown 报告，含：

* 工具调用按类型聚合（次数 + 参数字节数）；
* 助手文本产出量（最直接的上下文消耗）；
* "全量 dump"嫌疑：单次参数/输出体积异常的调用；
* 重复调用嫌疑：同一工具 + 同一参数反复出现；
* 建议清单：每条对应一个具体改法。

退出码：0 正常；1 参数/文件问题。
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

# 经验换算：英文约 4 字符/token，中文约 1.5 字符/token。
# 估算只用于**比较**（谁比谁大），不作为计费依据。
CJK_RANGES = ((0x4E00, 0x9FFF), (0x3000, 0x303F), (0xFF00, 0xFFEF))

TOOL_TEXT_NAMES = ("<text>", "<result>")


def estimate_tokens(text) -> int:
    """粗略估计 token 数：中文按 1.5 字符/token，其他按 4 字符/token。"""
    if not text:
        return 0
    if not isinstance(text, str):
        text = str(text)
    cjk = 0
    for ch in text:
        o = ord(ch)
        if any(lo <= o <= hi for lo, hi in CJK_RANGES):
            cjk += 1
    other = len(text) - cjk
    return int(cjk / 1.5 + other / 4) + 1


def text_len(obj) -> int:
    if obj is None:
        return 0
    if isinstance(obj, str):
        return len(obj)
    try:
        return len(json.dumps(obj, ensure_ascii=False))
    except Exception:
        return 0


def unwrap(obj):
    """Cursor transcript 的每行是 {"role":..,"message":{...}}，拆开外壳。"""
    if isinstance(obj, dict) and isinstance(obj.get("message"), dict):
        return obj["message"]
    return obj


def load_records(path):
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()

    stats = {"format": "jsonl", "lines": 0, "bytes": os.path.getsize(path)}

    stripped = raw.lstrip()
    if stripped.startswith("[") or (stripped.startswith("{") and '"messages"' in stripped[:200]):
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                obj = obj.get("messages") or obj.get("conversation") or []
            if isinstance(obj, list):
                stats["format"] = "json"
                return [unwrap(m) for m in obj], stats
        except Exception:
            pass

    msgs = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        stats["lines"] += 1
        try:
            obj = json.loads(line)
        except Exception:
            continue
        msgs.append(unwrap(obj))
    return msgs, stats


def iter_tool_calls(messages):
    """抽出 (tool_name, input_dict, output_len)。"""
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or msg.get("type") or ""
        content = msg.get("content")

        if isinstance(content, list):
            for blk in content:
                if not isinstance(blk, dict):
                    continue
                t = blk.get("type")
                if t in ("tool_use", "tool_call", "function_call"):
                    yield (
                        blk.get("name") or blk.get("tool_name") or "?",
                        blk.get("input") or blk.get("arguments") or {},
                        0,
                    )
                elif t in ("tool_result", "tool_output", "function_call_output"):
                    yield ("<result>", {}, text_len(blk.get("content") or blk.get("output")))
                elif t == "text":
                    yield ("<text>", {}, text_len(blk.get("text")))

        elif isinstance(content, str):
            if role == "tool" and msg.get("name"):
                yield ("<result>", {}, len(content))

        if msg.get("type") == "tool_use":
            yield (
                msg.get("name") or "?",
                msg.get("input") or msg.get("arguments") or {},
                0,
            )

        if msg.get("tool_name") and not isinstance(content, list):
            yield (msg.get("tool_name"), msg.get("tool_input") or msg.get("arguments") or {}, 0)

        for key in ("tool_results", "toolResults", "results"):
            arr = msg.get(key)
            if isinstance(arr, list):
                for r in arr:
                    if isinstance(r, dict):
                        yield ("<result>", {}, text_len(r.get("content") or r.get("output")))


def summarize(messages, dump_threshold=20000, top=15):
    by_tool = defaultdict(lambda: {"calls": 0, "in_chars": 0, "out_chars": 0})
    param_counter = Counter()
    big_calls = []
    last_tool = None

    for name, inp, res_len in iter_tool_calls(messages):
        if name == "<result>":
            # 结果归属到最近一次工具调用（粗略但足够定位大户）
            if last_tool and last_tool in by_tool:
                by_tool[last_tool]["out_chars"] += res_len
            continue

        if name == "<text>":
            by_tool["<text>"]["calls"] += 1
            by_tool["<text>"]["out_chars"] += res_len
            last_tool = None
            continue

        last_tool = name
        v = by_tool[name]
        v["calls"] += 1
        size = text_len(inp)
        v["in_chars"] += size
        try:
            key = name + "|" + json.dumps(inp, ensure_ascii=False, sort_keys=True)[:200]
        except Exception:
            key = name
        param_counter[key] += 1
        if size >= dump_threshold:
            big_calls.append((name, size, key))

    repeats = [(k, c) for k, c in param_counter.most_common() if c >= 3]

    ranked = sorted(
        by_tool.items(),
        key=lambda kv: -(kv[1]["out_chars"] + kv[1]["in_chars"]),
    )[:top]

    return {
        "by_tool": by_tool,
        "ranked": ranked,
        "repeats": repeats[:10],
        "big_calls": sorted(big_calls, key=lambda x: -x[1])[:10],
        "total_calls": sum(v["calls"] for v in by_tool.values() if v["calls"]),
        "tool_calls": sum(v["calls"] for k, v in by_tool.items() if k not in TOOL_TEXT_NAMES),
        "total_in": sum(v["in_chars"] for v in by_tool.values()),
        "total_out": sum(v["out_chars"] for v in by_tool.values()),
    }


def build_suggestions(s, dump_threshold):
    sug = []
    for name, size, key in s["big_calls"]:
        tool = key.split("|")[0]
        sug.append(
            "【大参数体】`%s` 单次传入 %d 字符（≥%d 阈值）。\n"
            "  → 改法：改用脚本切片/参数化查询；把长内容落文件，只在对话里传路径或摘要。"
            % (tool, size, dump_threshold)
        )
    for key, cnt in s["repeats"]:
        tool = key.split("|")[0]
        sug.append(
            "【重复调用】`%s` 用相同参数调了 %d 次。\n"
            "  → 改法：合并成一次批量脚本；或把第一次找到的锚点/参数写进常量，后续直接复用。"
            % (tool, cnt)
        )
    if s["total_calls"] > 300:
        sug.append(
            "【调用密度】总工具调用 %d 次，偏高。\n"
            "  → 改法：每 10 次调用自检一次（工具调用数 ÷ 用户消息数 > 30 即为空转）；"
            "把碎片化操作合成一个脚本一次性跑完。" % s["total_calls"]
        )
    if s["total_out"] > 500000:
        sug.append(
            "【上下文回灌】会话内累计输出 %d 字符（≈%d token）。\n"
            "  → 改法：原始 JSON / 日志落文件，对话里只保留 ≤30 行摘要；"
            "截图序列只读关键帧。" % (s["total_out"], estimate_tokens("x" * s["total_out"]))
        )
    if not sug:
        sug.append("未发现明显浪费模式。继续保持：一次探针拿全量结论、原始数据落文件、失败最多重试 2 次。")
    return sug


def render(s, stats, args):
    L = []
    L.append("# Token 成本画像报告\n")
    L.append("| 项 | 值 |")
    L.append("|---|---|")
    L.append("| 输入文件 | `%s` |" % args.input)
    L.append("| 格式 | %s（%d 行 / %.1f KB） |" % (stats["format"], stats["lines"], stats["bytes"] / 1024))
    L.append("| 工具调用总数 | %d |" % s["tool_calls"])
    L.append("| 累计参数字符 | %d |" % s["total_in"])
    L.append("| 累计输出字符 | %d（≈%d token） |" % (s["total_out"], estimate_tokens("x" * s["total_out"])))
    L.append("")

    L.append("## 按工具聚合\n")
    L.append("| 工具 | 调用数 | 参数字符 | 输出字符 | 估算 token |")
    L.append("|---|---:|---:|---:|---:|")
    for name, v in s["ranked"]:
        tok = estimate_tokens("x" * (v["out_chars"] + v["in_chars"]))
        L.append("| `%s` | %d | %d | %d | %d |" % (name, v["calls"], v["in_chars"], v["out_chars"], tok))
    L.append("")

    L.append("## 全量 dump 嫌疑（单次参数体过大）\n")
    if s["big_calls"]:
        L.append("| 工具 | 参数字符数 |")
        L.append("|---|---:|")
        for name, size, _ in s["big_calls"]:
            L.append("| `%s` | %d |" % (name, size))
    else:
        L.append("无（没有单次参数超过 `--dump-threshold`）。")
    L.append("")

    L.append("## 重复调用嫌疑\n")
    if s["repeats"]:
        L.append("| 工具 + 参数 | 次数 |")
        L.append("|---|---:|")
        for key, cnt in s["repeats"]:
            short = key if len(key) <= 110 else key[:107] + "..."
            L.append("| `%s` | %d |" % (short.replace("|", "\\|"), cnt))
    else:
        L.append("无（没有同一工具 + 相同参数重复 3 次以上）。")
    L.append("")

    L.append("## 建议清单\n")
    for i, t in enumerate(build_suggestions(s, args.dump_threshold), 1):
        L.append("%d. %s\n" % (i, t))

    L.append("---\n")
    L.append("> 估算说明：中文按 1.5 字符/token、其他按 4 字符/token 换算，"
             "仅用于**比较各工具之间的相对开销**，不作为计费依据。\n"
             "> 注意：Cursor transcript 通常不记录工具返回体的完整内容，"
             "本报告的「输出字符」只统计记录里可见的文本（助手输出 + 已记录的 tool_result）。")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="分析会话 token 消耗与工具调用画像")
    ap.add_argument("--input", required=True, help="会话记录路径（JSONL 或 JSON）")
    ap.add_argument("--out", help="报告输出路径（.md）；不给则打印到标准输出")
    ap.add_argument("--top", type=int, default=15, help="按体量排前 N 个工具（默认 15）")
    ap.add_argument("--dump-threshold", type=int, default=20000,
                    help="单次参数超过该字符数算「全量 dump」（默认 20000）")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.input):
        print("✗ 文件不存在: %s" % args.input, file=sys.stderr)
        return 1

    try:
        messages, stats = load_records(args.input)
    except Exception as e:
        print("✗ 读取失败: %s" % e, file=sys.stderr)
        return 1

    if not messages:
        print("✗ 没解析出任何消息 —— 文件格式可能不是会话记录", file=sys.stderr)
        return 1

    s = summarize(messages, dump_threshold=args.dump_threshold, top=args.top)
    report = render(s, stats, args)

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report)
        print("报告 -> %s" % args.out)
    else:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
