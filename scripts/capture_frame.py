#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""capture_frame.py —— 本地 HTTP 接收端，抓 WebGL 的真实帧。

为什么需要
----------
WebGL 在 ``preserveDrawingBuffer: false``（three.js / Babylon 默认）时，
浏览器截图接口在渲染循环之外取缓冲区，拿到的是**空帧**（纯黑或纯白）。
本脚本起一个本地 HTTP 服务，页面里的 JS 把 canvas 的 dataURL POST 给它，
它把 base64 解码落盘成 PNG —— 抓到的一定是"刚刚渲染出来的"那一帧。

用法
----
起服务（终端 A）::

    python capture_frame.py --out-dir shots --port 8765

页面侧（终端 B / 浏览器控制台）::

    fetch('http://127.0.0.1:8765/shot?name=frame01', {
      method: 'POST',
      body: JSON.stringify({ dataUrl: canvas.toDataURL('image/png') })
    });

要点
----
* 跨域：默认发 ``Access-Control-Allow-Origin: *``。
* 落盘文件名 = ``--out-dir`` + ``name`` 参数 + ``.png``。
* ``--port`` 被占用时会自动找下一个空闲端口并打印实际端口。
* 抓完先看文件大小：``< 5 KB`` 基本就是空帧。

退出码：0 正常；2 参数问题。
"""

import argparse
import base64
import binascii
import json
import os
import re
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")
MAX_BODY = 64 * 1024 * 1024  # 64MB 上限，防止 base64 大图把内存打爆


def safe_name(name: str, fallback: str = "frame") -> str:
    """把用户给的 name 变成安全的文件名片段（防路径穿越）。"""
    name = SAFE_NAME.sub("_", (name or "").strip())
    name = name.strip("._") or fallback
    return name[:120]


def find_free_port(preferred: int) -> int:
    """优先用 preferred；被占用则找一个空闲端口。"""
    for port in [preferred] + list(range(preferred + 1, preferred + 50)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("找不到可用端口（尝试了 %d..%d）" % (preferred, preferred + 50))


def decode_data_url(data_url: str) -> bytes:
    """从 dataURL 里取出图片字节。"""
    if not data_url:
        raise ValueError("dataUrl 为空")
    if "," not in data_url:
        raise ValueError("dataUrl 格式不对（缺少逗号分隔符）")
    head, payload = data_url.split(",", 1)
    if "base64" in head:
        try:
            return base64.b64decode(payload, validate=False)
        except (binascii.Error, ValueError) as e:
            raise ValueError("base64 解码失败: %s" % e)
    from urllib.parse import unquote_to_bytes

    return unquote_to_bytes(payload)


class Handler(BaseHTTPRequestHandler):
    out_dir = "shots"
    server_version = "capture_frame/1.0"

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):  # noqa: N802
        """健康检查，顺便告诉调用方该 POST 到哪个 URL。"""
        if self.path.rstrip("/") in ("", "/shot"):
            body = json.dumps(
                {"ok": True, "post_to": "/shot?name=<name>", "out_dir": self.out_dir},
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._cors()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self._cors()
        self.end_headers()

    def do_POST(self):  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._reply(400, {"ok": False, "error": "请求体为空或超过 %d MB" % (MAX_BODY >> 20)})
            return

        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception as e:
            self._reply(400, {"ok": False, "error": "JSON 解析失败: %s" % e})
            return
        if not isinstance(payload, dict):
            self._reply(400, {"ok": False, "error": "请求体必须是 JSON 对象"})
            return

        # name 可以放在 URL query 或 JSON body 里
        name = payload.get("name") or ""
        if "name=" in self.path:
            from urllib.parse import parse_qs, urlparse

            q = parse_qs(urlparse(self.path).query)
            if q.get("name"):
                name = q["name"][0]

        data_url = payload.get("dataUrl") or payload.get("data_url") or ""
        try:
            data = decode_data_url(data_url)
        except ValueError as e:
            self._reply(400, {"ok": False, "error": str(e)})
            return

        if not data:
            self._reply(400, {"ok": False, "error": "解码后图片为空（可能是空帧）"})
            return

        os.makedirs(self.out_dir, exist_ok=True)
        out_path = os.path.join(self.out_dir, safe_name(name) + ".png")
        with open(out_path, "wb") as f:
            f.write(data)

        # 落盘后立刻给回执，让调用方知道文件路径和体积
        hint = ""
        if len(data) < 5 * 1024:
            hint = " ⚠ 文件很小(<5KB)，多半是空帧或黑帧"
        print("[capture] 收到 %s (%d bytes) -> %s%s" % (name or "(unnamed)", len(data), out_path, hint),
              flush=True)
        self._reply(200, {"ok": True, "path": out_path, "bytes": len(data), "hint": hint.strip()})

    def _reply(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # 静音默认访问日志
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="本地 HTTP 接收端：抓 WebGL 真实帧（dataURL -> PNG）")
    ap.add_argument("--out-dir", default="shots", help="PNG 落盘目录（默认: shots）")
    ap.add_argument("--port", type=int, default=8765, help="监听端口，被占用时自动顺延（默认: 8765）")
    ap.add_argument("--once", action="store_true", help="收到一帧后自动退出")
    args = ap.parse_args(argv)

    try:
        port = find_free_port(args.port)
    except RuntimeError as e:
        print("✗ %s" % e, file=sys.stderr)
        return 2

    Handler.out_dir = os.path.abspath(args.out_dir)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)

    print("capture_frame 服务已启动")
    print("  POST http://127.0.0.1:%d/shot?name=<name>" % port)
    print("  落盘目录: %s" % Handler.out_dir)
    if port != args.port:
        print("  （端口 %d 被占用，已自动顺延到 %d）" % (args.port, port))
    print("  Ctrl+C 停止")

    if args.once:
        # 收到一帧后退出：后台跑服务，主线程等首个有效落盘
        import time

        server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        server_thread.start()

        deadline = time.time() + 120
        before = set(os.listdir(Handler.out_dir)) if os.path.isdir(Handler.out_dir) else set()
        try:
            while time.time() < deadline:
                time.sleep(0.3)
                if not os.path.isdir(Handler.out_dir):
                    continue
                now = set(os.listdir(Handler.out_dir))
                if now - before:
                    print("✓ 已收到一帧，自动退出")
                    break
        except KeyboardInterrupt:
            pass
        httpd.shutdown()
        httpd.server_close()
        return 0

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
