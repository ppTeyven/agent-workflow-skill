#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""crop_compare.py —— A/B 并排对比 + 关键区域裁切放大。

为什么需要
----------
* 小图上看不清接触阴影、边缘锯齿、贴图接缝 —— 必须裁切放大 2~4 倍。
* 改一个开关后只看新图不够 —— 新旧两张要**同一区域**裁切后并排放大，
  才能看清"到底出没出来"。若 A/B 看不出差异，说明改动根本没生效
  （常见真因：改动被别的逻辑覆盖、静态服务器缓存）。

用法
----
只裁切放大一张::

    python crop_compare.py --left before.png --out crop.png --region 30,40,70,80 --scale 3

A/B 并排对比::

    python crop_compare.py --left before.png --right after.png \
        --region 0,0,100,100 --scale 2 --out ab.png --sep magenta

``--region`` 是**百分比**坐标 ``left,top,right,bottom``（0~100），
两张图必须用**同一区域**（像素级对齐），否则对比无意义。

退出码：0 正常；1 参数/文件问题；2 缺 Pillow。
"""

import argparse
import os
import sys

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    print("✗ 需要 Pillow：pip install pillow", file=sys.stderr)
    raise SystemExit(2)

SEP_COLORS = {
    "magenta": (255, 0, 255),
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "cyan": (0, 255, 255),
    "white": (255, 255, 255),
    "black": (0, 0, 0),
}


def parse_region(text):
    """'30,40,70,80' -> (0.30, 0.40, 0.70, 0.80) 百分比小数。"""
    parts = [p.strip() for p in text.replace(" ", "").split(",") if p.strip()]
    if len(parts) != 4:
        raise ValueError("--region 需要 4 个数字：left,top,right,bottom（百分比 0~100）")
    vals = []
    for p in parts:
        v = float(p)
        if not (0.0 <= v <= 100.0):
            raise ValueError("--region 每个值必须在 0~100 之间，收到 %s" % p)
        vals.append(v / 100.0)
    l, t, r, b = vals
    if r <= l or b <= t:
        raise ValueError("--region 的 right/bottom 必须大于 left/top")
    return l, t, r, b


def crop_scale(im, region, scale):
    """按百分比区域裁切并放大。"""
    l, t, r, b = region
    W, H = im.size
    box = (int(W * l), int(H * t), int(W * r), int(H * b))
    box = (
        max(0, min(box[0], W - 1)),
        max(0, min(box[1], H - 1)),
        max(box[2], box[0] + 1),
        max(box[3], box[1] + 1),
    )
    im2 = im.crop(box)
    if scale and scale != 1:
        im2 = im2.resize(
            (max(1, int(im2.width * scale)), max(1, int(im2.height * scale))),
            Image.LANCZOS,
        )
    return im2


def diff_ratio(a, b):
    """两图差异像素占比（粗略指标，只用来判断"有没有变化"，不替代眼睛）。"""
    if a.size != b.size:
        return None
    ga = a.convert("RGB")
    gb = b.convert("RGB")
    wa, hb = ga.size
    step = max(1, int((wa * hb) ** 0.5) // 64)  # 采样，够用且快
    diff = 0
    total = 0
    for y in range(0, hb, step):
        for x in range(0, wa, step):
            total += 1
            if ga.getpixel((x, y)) != gb.getpixel((x, y)):
                diff += 1
    return diff / total if total else 0.0


def main(argv=None):
    ap = argparse.ArgumentParser(description="A/B 并排对比 + 关键区域裁切放大")
    ap.add_argument("--left", required=True, help="左图（修复前 / before）")
    ap.add_argument("--right", help="右图（修复后 / after）；不给则只输出裁切放大图")
    ap.add_argument("--out", required=True, help="输出 PNG")
    ap.add_argument("--region", default="0,0,100,100", help="裁切区域百分比 left,top,right,bottom")
    ap.add_argument("--scale", type=float, default=2.0, help="放大倍数（默认 2）")
    ap.add_argument("--sep", default="magenta", choices=sorted(SEP_COLORS), help="A/B 中间分隔线颜色")
    ap.add_argument("--sep-width", type=int, default=4, help="分隔线像素宽（默认 4）")
    args = ap.parse_args(argv)

    try:
        region = parse_region(args.region)
    except ValueError as e:
        print("✗ %s" % e, file=sys.stderr)
        return 1

    for p in [args.left] + ([args.right] if args.right else []):
        if not os.path.isfile(p):
            print("✗ 文件不存在: %s" % p, file=sys.stderr)
            return 1

    try:
        left = crop_scale(Image.open(args.left).convert("RGB"), region, args.scale)
    except Exception as e:
        print("✗ 读取 %s 失败: %s" % (args.left, e), file=sys.stderr)
        return 1

    if not args.right:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        left.save(args.out)
        print("已裁切放大 -> %s  (%dx%d)" % (args.out, left.width, left.height))
        return 0

    right = crop_scale(Image.open(args.right).convert("RGB"), region, args.scale)

    # 两张图尺寸不一致时（A/B 不对齐），把右图缩放到左图尺寸 —— 但要警告
    if right.size != left.size:
        right = right.resize(left.size, Image.LANCZOS)
        print("⚠ 两图原始尺寸不同，已把右图缩放到左图尺寸 —— 对比结果仅供参考")

    w = left.width + right.width + args.sep_width
    h = max(left.height, right.height)
    canvas = Image.new("RGB", (w, h), SEP_COLORS[args.sep])
    canvas.paste(left, (0, 0))
    canvas.paste(right, (left.width + args.sep_width, 0))

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    canvas.save(args.out)

    print("A/B 对比图 -> %s  (%dx%d)" % (args.out, canvas.width, canvas.height))
    print("  左 = %s   右 = %s" % (args.left, args.right))
    print("  区域 %s  放大 %.1fx" % (args.region, args.scale))

    try:
        ratio = diff_ratio(left, right)
        if ratio is not None:
            if ratio < 0.005:
                print("  ⚠ 差异像素占比 %.2f%% —— **几乎没变化**：改动可能没生效，"
                      "回去查是否被别的逻辑覆盖 / 缓存" % (ratio * 100))
            else:
                print("  差异像素占比 %.2f%% —— 有变化，但仍需亲眼看图确认变化是对的" % (ratio * 100))
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
