#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""假 ESP32-S3 探针节点（没有硬件时自测 PC 侧脚本用）。

模拟：UDP 四时间戳回显 + HTTP /rssi /info /sink /echo。
可以把 RTT/抖动/丢包/信号做成"走进遮挡区"的曲线，用来验证 pc/probe_link.py。

用法：
    # 好链路（RTT 20 ms）
    python tools/fake_esp32_probe.py

    # 模拟"越走越差"：RTT 从 25 ms 涨到 180 ms、丢包 8%、RSSI 从 -45 掉到 -78
    python tools/fake_esp32_probe.py --pattern degrade

    # 配合探针
    python pc/probe_link.py --host 127.0.0.1 --udp-port 9000 --http-port 8080 --duration 10 --label fake
"""
from __future__ import annotations

import argparse
import json
import math
import random
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PKT_REQ = struct.Struct("<Iq")
PKT_RSP = struct.Struct("<Iqqqh")


def esp_us() -> int:
    return time.monotonic_ns() // 1000


class Sim:
    def __init__(self, args):
        self.args = args
        self.t0 = time.monotonic()
        self.start_rssi = args.rssi

    def profile(self):
        """返回 (额外 RTT ms, 丢包率, rssi)。"""
        a = self.args
        elapsed = time.monotonic() - self.t0
        if a.pattern == "good":
            return a.base_rtt_ms, a.loss, self.start_rssi
        if a.pattern == "degrade":
            k = min(1.0, elapsed / max(1.0, a.ramp_s))
            rtt = a.base_rtt_ms + (a.bad_rtt_ms - a.base_rtt_ms) * k
            rssi = int(self.start_rssi + (-35) * k)
            return rtt, a.loss, rssi
        # oscillate
        k = 0.5 + 0.5 * math.sin(elapsed / max(0.5, a.ramp_s) * 2 * math.pi)
        rtt = a.base_rtt_ms + (a.bad_rtt_ms - a.base_rtt_ms) * k
        rssi = int(self.start_rssi - 35 * k)
        return rtt, a.loss, rssi


def udp_server(args, sim: Sim, stop: threading.Event):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.bind, args.udp_port))
    sock.settimeout(0.3)
    print(f"[udp] listening {args.bind}:{args.udp_port}  pattern={args.pattern}")
    while not stop.is_set():
        try:
            data, addr = sock.recvfrom(64)
        except socket.timeout:
            continue
        except OSError:
            break
        if len(data) < PKT_REQ.size:
            continue
        seq, t0 = PKT_REQ.unpack_from(data)
        rtt_extra, loss, rssi = sim.profile()
        if random.random() < loss:
            continue  # 模拟丢包：不回
        # 网络时延要发生在 t1 之前（下行）和 t2 之后（上行），
        # 否则会被 RTT 公式当作"板子处理时间"减掉。
        half = max(0.0, rtt_extra / 2000.0)
        time.sleep(max(0.0, half + random.gauss(0, args.jitter_ms / 2000.0)))
        t1 = esp_us()
        t2 = esp_us()          # 处理时间≈0
        time.sleep(max(0.0, half + random.gauss(0, args.jitter_ms / 2000.0)))
        reply = struct.pack("<Iqqqh", seq, t0, t1, t2, rssi)
        try:
            sock.sendto(reply, addr)
        except OSError:
            break
    sock.close()


class Handler(BaseHTTPRequestHandler):
    sim: Sim = None

    def log_message(self, *a):
        pass

    def _json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        _, _, rssi = self.sim.profile()
        if u.path == "/rssi":
            self._json({"rssi": rssi, "ch": 6, "ip": "127.0.0.1", "fake": True})
        elif u.path == "/info":
            self._json({"chip": "fake-esp32s3", "cores": 2, "heap": 260000,
                        "psram": 8 * 1024 * 1024, "rssi": rssi})
        elif u.path == "/echo":
            kb = int(parse_qs(u.query).get("kb", ["8"])[0])
            body = b"A" * (kb * 1024)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._json({"ok": True, "path": u.path})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        t0 = time.monotonic()
        left = n
        while left > 0:
            chunk = self.rfile.read(min(left, 65536))
            if not chunk:
                break
            left -= len(chunk)
        dt = time.monotonic() - t0
        _, _, rssi = self.sim.profile()
        self._json({"bytes": n - left, "ms": int(dt * 1000), "rssi": rssi,
                    "kbps": int((n - left) * 8 / dt / 1000) if dt else 0})


def main(argv=None):
    ap = argparse.ArgumentParser(description="假 ESP32-S3 探针节点")
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--udp-port", type=int, default=9000)
    ap.add_argument("--http-port", type=int, default=8080)
    ap.add_argument("--pattern", choices=["good", "degrade", "oscillate"], default="good")
    ap.add_argument("--base-rtt-ms", type=float, default=20.0)
    ap.add_argument("--bad-rtt-ms", type=float, default=180.0)
    ap.add_argument("--jitter-ms", type=float, default=3.0)
    ap.add_argument("--loss", type=float, default=0.0)
    ap.add_argument("--rssi", type=int, default=-45)
    ap.add_argument("--ramp-s", type=float, default=20.0, help="恶化/振荡的时间尺度")
    args = ap.parse_args(argv)

    sim = Sim(args)
    stop = threading.Event()
    t = threading.Thread(target=udp_server, args=(args, sim, stop), daemon=True)
    t.start()

    Handler.sim = sim
    httpd = ThreadingHTTPServer((args.bind, args.http_port), Handler)
    print(f"[http] listening {args.bind}:{args.http_port}")
    print("Ctrl+C 退出")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
