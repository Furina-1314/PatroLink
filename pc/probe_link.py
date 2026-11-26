#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PatroLink 链路探针（PC 侧）—— 无第三方依赖即可运行。

向 ESP32-S3 探针节点发 UDP 包（seq + 客户端 t0），用四时间戳算 RTT：
    RTT = (t3 - t0) - (t2 - t1)      # t1/t2 是 ESP32 收到/发出时刻
这样不需要两端对时。

用法：
    # 30 秒，20 Hz，标注"桌边"
    python pc/probe_link.py --host 192.168.137.2 --duration 30 --label desk

    # 顺带测一下上行/下行吞吐（传 32 KB）
    python pc/probe_link.py --host 192.168.137.2 --duration 10 --sink-kb 32 --echo-kb 32

    # 没硬件时先用假节点自测
    python tools/fake_esp32_probe.py            # 另开一个终端
    python pc/probe_link.py --host 127.0.0.1 --duration 5 --label fake

输出：
    traces/probe_<label>_<ts>.csv    逐包记录（wall_ms, seq, rtt_ms, rssi_dbm, lost）
    traces/probe_<label>_<ts>.json   汇总（中位数/P95/丢包率/RSSI）
    traces/probe_<label>_<ts>.png    RTT 与 RSSI 曲线（装了 matplotlib 才会生成）
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import socket
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

PKT_REQ = struct.Struct("<Iq")       # seq(uint32), t0_us(int64)      = 12 B
PKT_RSP = struct.Struct("<Iqqqh")    # seq, t0, t1, t2, rssi(int16)   = 30 B


def now_us() -> int:
    return time.monotonic_ns() // 1000


def wall_ms() -> int:
    return int(time.time() * 1000)


def pct(values, q):
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def probe_udp(host: str, port: int, hz: float, duration: float, timeout: float,
              progress=None, progress_every: float = 5.0):
    """返回逐包记录列表。

    progress: 可选回调 f(records)，每 progress_every 秒调用一次，用于实时观察。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
    except OSError as exc:
        raise SystemExit(f"连不上 {host}:{port}：{exc}")

    interval = 1.0 / hz
    records = []
    seq = 0
    t_end = time.monotonic() + duration
    t_next = time.monotonic()
    last_report = time.monotonic()
    while time.monotonic() < t_end:
        seq += 1
        t0 = now_us()
        wall = wall_ms()
        sock.send(PKT_REQ.pack(seq, t0))
        rtt_ms, rssi = None, None
        try:
            data = sock.recv(64)
            t3 = now_us()
            if len(data) >= PKT_RSP.size:
                r_seq, r_t0, t1, t2, r_rssi = PKT_RSP.unpack_from(data)
                if r_seq == seq:
                    rtt_ms = ((t3 - r_t0) - (t2 - t1)) / 1000.0
                    rssi = r_rssi
        except socket.timeout:
            pass
        records.append(dict(wall_ms=wall, seq=seq, rtt_ms=rtt_ms, rssi_dbm=rssi,
                            lost=1 if rtt_ms is None else 0))
        if progress is not None and time.monotonic() - last_report >= progress_every:
            progress(records)
            last_report = time.monotonic()
        t_next += interval
        sleep = t_next - time.monotonic()
        if sleep > 0:
            time.sleep(sleep)
    sock.close()
    return records


def http_post(host: str, port: int, path: str, payload: bytes, timeout: float = 10.0):
    """不用 requests，直接用 http.client。返回 (字节数, 耗时秒)。"""
    import http.client

    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    t0 = time.monotonic()
    try:
        conn.request("POST", path, body=payload,
                     headers={"Content-Type": "application/octet-stream"})
        resp = conn.getresponse()
        body = resp.read()
        dt = time.monotonic() - t0
        return len(payload), dt, body
    finally:
        conn.close()


def http_get(host: str, port: int, path: str, timeout: float = 10.0):
    import http.client

    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    t0 = time.monotonic()
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        dt = time.monotonic() - t0
        return len(body), dt, body
    finally:
        conn.close()


def summarize(records):
    got = [r for r in records if not r["lost"]]
    rtts = [r["rtt_ms"] for r in got]
    rssis = [r["rssi_dbm"] for r in got if r["rssi_dbm"] is not None]
    jit = [abs(rtts[i] - rtts[i - 1]) for i in range(1, len(rtts))]
    return {
        "sent": len(records),
        "received": len(got),
        "loss_rate": round(1 - len(got) / max(1, len(records)), 4),
        "rtt_ms": {
            "median": round(pct(rtts, 0.5), 2),
            "p95": round(pct(rtts, 0.95), 2),
            "min": round(min(rtts), 2) if rtts else None,
            "max": round(max(rtts), 2) if rtts else None,
            "jitter_median": round(pct(jit, 0.5), 2),
        },
        "rssi_dbm": {
            "median": round(pct(rssis, 0.5), 1),
            "min": min(rssis) if rssis else None,
            "max": max(rssis) if rssis else None,
        },
    }


def save_csv(path: Path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["wall_ms", "seq", "rtt_ms", "rssi_dbm", "lost"])
        for r in records:
            w.writerow([r["wall_ms"], r["seq"],
                        "" if r["rtt_ms"] is None else f"{r['rtt_ms']:.3f}",
                        "" if r["rssi_dbm"] is None else r["rssi_dbm"],
                        r["lost"]])


def save_plot(path: Path, records, label: str, host: str):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        try:
            plt.rcParams["font.family"] = "Microsoft YaHei"
            plt.rcParams["axes.unicode_minus"] = False
        except Exception:
            pass
    except ImportError:
        print("（未安装 matplotlib，跳过画图）")
        return False

    t = [(r["wall_ms"] - records[0]["wall_ms"]) / 1000.0 for r in records]
    rtt = [r["rtt_ms"] if not r["lost"] else None for r in records]
    rssi = [r["rssi_dbm"] if not r["lost"] else None for r in records]

    fig, axes = plt.subplots(2, 1, figsize=(10, 6), sharex=True)
    axes[0].plot(t, rtt, ".", markersize=3, color="#2F5597", label="RTT")
    rtt_ok = [v for v in rtt if v is not None]
    if rtt_ok:
        m = sorted(rtt_ok)[len(rtt_ok) // 2]
        axes[0].axhline(m, color="#C55A11", linewidth=1, linestyle="--", label=f"median {m:.1f} ms")
    axes[0].set_ylabel("RTT (ms)")
    axes[0].legend(loc="upper right", fontsize=8)
    axes[0].grid(alpha=0.3)
    axes[0].set_title(f"PatroLink 链路探针  host={host}  label={label}")

    axes[1].plot(t, rssi, ".", markersize=3, color="#1F7A6C", label="RSSI")
    axes[1].set_ylabel("RSSI (dBm)")
    axes[1].set_xlabel("时间 (s)")
    axes[1].legend(loc="upper right", fontsize=8)
    axes[1].grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="PatroLink 链路探针（UDP 四时间戳测 RTT）")
    ap.add_argument("--host", required=True, help="ESP32-S3 的 IP，如 192.168.137.2")
    ap.add_argument("--udp-port", type=int, default=9000)
    ap.add_argument("--http-port", type=int, default=80)
    ap.add_argument("--hz", type=float, default=20.0, help="探针频率（默认 20 Hz）")
    ap.add_argument("--duration", type=float, default=30.0, help="测量时长（秒）")
    ap.add_argument("--timeout", type=float, default=0.2, help="单包等待（秒）")
    ap.add_argument("--label", default="unlabeled",
                    help="场景标签，如 good_desk / occlude_wall / crowd_corridor / outage_far")
    # ---- 数据集元数据（不进 CSV，只写 JSON；用于复现与后续特征分析）----
    ap.add_argument("--distance-m", type=float, default=None, help="收发距离（米）")
    ap.add_argument("--obstacle", default="none",
                    help="遮挡物：none / wall / door / pillar / person / floor")
    ap.add_argument("--channel", type=int, default=None, help="WiFi 信道")
    ap.add_argument("--device", default="esp32s3", help="探针设备")
    ap.add_argument("--hotspot", default="pc", help="热点来源：pc / phone / router")
    ap.add_argument("--note", default="", help="备注（走动方式、环境等）")
    ap.add_argument("--out", default="traces", help="输出目录")
    ap.add_argument("--sink-kb", type=int, default=0, help="额外测上行吞吐：POST N KB")
    ap.add_argument("--echo-kb", type=int, default=0, help="额外测下行吞吐：GET N KB")
    ap.add_argument("--progress", type=float, default=5.0,
                    help="实时打印进度的间隔（秒），0 表示关闭")
    ap.add_argument("--no-plot", action="store_true")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print(f"探针 → {args.host}:{args.udp_port}  {args.hz:g} Hz × {args.duration:g} s  label={args.label}")
    print(f"  元数据：distance={args.distance_m} m obstacle={args.obstacle} "
          f"channel={args.channel} hotspot={args.hotspot}")

    def _progress(recs):
        got = [r for r in recs if not r["lost"]]
        rtts = [r["rtt_ms"] for r in got]
        rssis = [r["rssi_dbm"] for r in got if r["rssi_dbm"] is not None]
        elapsed = (recs[-1]["wall_ms"] - recs[0]["wall_ms"]) / 1000.0
        med = pct(rtts, 0.5) if rtts else float("nan")
        rs = pct(rssis, 0.5) if rssis else float("nan")
        print(f"  [{elapsed:6.0f}s] 收 {len(got)}/{len(recs)} "
              f"丢 {1 - len(got) / max(1, len(recs)):5.1%} ｜ "
              f"RTT 中位 {med:6.1f} ms ｜ RSSI {rs:5.0f} dBm")

    records = probe_udp(args.host, args.udp_port, args.hz, args.duration, args.timeout,
                        progress=_progress if args.progress > 0 else None,
                        progress_every=args.progress if args.progress > 0 else 5.0)
    s = summarize(records)

    print("\n──── 结果 ────")
    print(f"  发送 {s['sent']} 包 ｜ 收到 {s['received']} ｜ 丢包 {s['loss_rate']:.1%}")
    r = s["rtt_ms"]
    print(f"  RTT 中位数 {r['median']} ms ｜ P95 {r['p95']} ms ｜ 最小 {r['min']} ｜ 最大 {r['max']}")
    print(f"  抖动（相邻差中位数）{r['jitter_median']} ms")
    rs = s["rssi_dbm"]
    print(f"  RSSI 中位数 {rs['median']} dBm ｜ 范围 {rs['min']} ~ {rs['max']} dBm")

    if args.sink_kb:
        payload = os.urandom(args.sink_kb * 1024)
        try:
            n, dt, body = http_post(args.host, args.http_port, "/sink", payload)
            mbps = n * 8 / dt / 1e6
            print(f"  上行吞吐：{n/1024:.0f} KB / {dt*1000:.0f} ms → {mbps:.2f} Mbps  "
                  f"（板子回执 {body[:80].decode('utf-8', 'ignore')}）")
            s["upload"] = {"bytes": n, "ms": round(dt * 1000, 1), "mbps": round(mbps, 3)}
        except Exception as exc:
            print(f"  上行吞吐测试失败：{exc}")
    if args.echo_kb:
        try:
            n, dt, _ = http_get(args.host, args.http_port, f"/echo?kb={args.echo_kb}")
            mbps = n * 8 / dt / 1e6
            print(f"  下行吞吐：{n/1024:.0f} KB / {dt*1000:.0f} ms → {mbps:.2f} Mbps")
            s["download"] = {"bytes": n, "ms": round(dt * 1000, 1), "mbps": round(mbps, 3)}
        except Exception as exc:
            print(f"  下行吞吐测试失败：{exc}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    stamp = f"{args.label}_{ts}"
    out = Path(args.out)
    csv_path = out / f"probe_{stamp}.csv"
    json_path = out / f"probe_{stamp}.json"
    save_csv(csv_path, records)
    meta = {
        "host": args.host, "label": args.label, "hz": args.hz,
        "duration_s": args.duration,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "dataset": {
            "distance_m": args.distance_m,
            "obstacle": args.obstacle,
            "channel": args.channel,
            "device": args.device,
            "hotspot": args.hotspot,
            "note": args.note,
        },
        "summary": s,
    }
    json_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  已保存：{csv_path}")
    print(f"          {json_path}")
    if not args.no_plot:
        png_path = out / f"probe_{stamp}.png"
        if save_plot(png_path, records, args.label, args.host):
            print(f"          {png_path}")

    # 判据提示（W1 验收）
    if s["received"] >= 0.9 * max(1, s["sent"]) and r["median"] is not None:
        print("\n  ✓ 本轮可用：把这条 trace 按位置标签存好，走到不同位置再来一轮")
    else:
        print("\n  ! 丢包率偏高：检查热点/距离/防火墙，或先把 --hz 降到 5 再试")
    return 0


if __name__ == "__main__":
    sys.exit(main())
