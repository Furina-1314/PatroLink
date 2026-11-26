#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按计划逐条采集链路 trace（明天照着跑就行）。

它做三件事：
    1) 按 plans/collect_plan.json 依次提示"换位置 → 按 Enter"；
    2) 调 pc/probe_link.py 的测量函数，落盘 CSV/JSON/PNG；
    3) 每条采集完往 traces/manifest.csv 追加一行（含距离/遮挡/注入参数）。

用法：
    # 先看计划（不测）
    python pc/collect_dataset.py --host 192.168.137.2 --dry-run

    # 正式采集（每条开始前按 Enter；--yes 则不等）
    python pc/collect_dataset.py --host 192.168.137.2 --yes

    # 只采两条试试
    python pc/collect_dataset.py --host 192.168.137.2 --only wall_5m,person_3m --limit 2

    # CI / 无硬件自测（配合 tools/fake_esp32_probe.py）
    python pc/collect_dataset.py --host 127.0.0.1 --plan plans/ci_plan.json --yes --no-plot --out /tmp/collect
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_link import pct, probe_udp, save_csv, save_plot  # noqa: E402

MANIFEST_COLS = [
    "started_at", "label", "csv", "json", "duration_s", "distance_m", "obstacle",
    "channel", "hotspot", "inject", "note", "sent", "received", "loss_rate",
    "rtt_median", "rtt_p95", "rssi_median",
]


def load_plan(path: Path):
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return data["runs"]


def append_manifest(path: Path, row: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_COLS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in MANIFEST_COLS})


def progress_printer(label: str):
    def _p(recs):
        got = [r for r in recs if not r["lost"]]
        rtts = [r["rtt_ms"] for r in got]
        rssis = [r["rssi_dbm"] for r in got if r["rssi_dbm"] is not None]
        elapsed = (recs[-1]["wall_ms"] - recs[0]["wall_ms"]) / 1000.0
        med = pct(rtts, 0.5) if rtts else float("nan")
        rs = pct(rssis, 0.5) if rssis else float("nan")
        print(f"    [{elapsed:6.0f}s] {label:16s} 收 {len(got)}/{len(recs)} "
              f"丢 {1 - len(got) / max(1, len(recs)):5.1%} ｜ "
              f"RTT {med:6.1f} ms ｜ RSSI {rs:5.0f} dBm")
    return _p


def run_one(run: dict, args) -> dict:
    label = run["label"]
    duration = float(run.get("duration_s", 300))
    meta = {
        "distance_m": run.get("distance_m"),
        "obstacle": run.get("obstacle", "none"),
        "channel": run.get("channel"),
        "device": run.get("device", "esp32s3"),
        "hotspot": run.get("hotspot", "pc"),
        "note": run.get("note", ""),
        "inject": run.get("inject"),
    }
    print(f"\n{'='*64}")
    print(f"[{label}] 时长 {duration:g}s ｜ 距离 {meta['distance_m']} m ｜ "
          f"遮挡 {meta['obstacle']} ｜ 信道 {meta['channel']} ｜ 热点 {meta['hotspot']}")
    if meta["note"]:
        print(f"  备注：{meta['note']}")
    if meta["inject"]:
        inj = meta["inject"]
        print(f"  ⚠ 可控注入：{inj.get('tool', 'Clumsy')} → "
              f"delay={inj.get('delay_ms')} ms / loss={inj.get('loss_pct')}% / "
              f"jitter={inj.get('jitter_ms')} ms")
        print("    请先在 Clumsy 里设好（Filter: udp and udpport 9000），再继续。")
    if not args.yes:
        input("  准备好后按 Enter 开始采集（Ctrl+C 中止）... ")

    started = datetime.now()
    records = probe_udp(args.host, args.udp_port, args.hz, duration, args.timeout,
                        progress=progress_printer(label) if args.progress > 0 else None,
                        progress_every=args.progress if args.progress > 0 else 5.0)
    got = [r for r in records if not r["lost"]]
    rtts = [r["rtt_ms"] for r in got]
    rssis = [r["rssi_dbm"] for r in got if r["rssi_dbm"] is not None]
    summary = {
        "sent": len(records), "received": len(got),
        "loss_rate": round(1 - len(got) / max(1, len(records)), 4),
        "rtt_median": round(pct(rtts, 0.5), 2) if rtts else None,
        "rtt_p95": round(pct(rtts, 0.95), 2) if rtts else None,
        "rssi_median": round(pct(rssis, 0.5), 1) if rssis else None,
        "jitter_median": round(pct([abs(rtts[i] - rtts[i - 1]) for i in range(1, len(rtts))], 0.5), 2)
        if len(rtts) > 1 else None,
    }

    ts = started.strftime("%Y%m%d_%H%M%S")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    csv_path = out / f"probe_{label}_{ts}.csv"
    json_path = out / f"probe_{label}_{ts}.json"
    save_csv(csv_path, records)
    payload = {
        "host": args.host, "label": label, "hz": args.hz, "duration_s": duration,
        "started_at": started.isoformat(timespec="seconds"),
        "dataset": meta, "summary": summary,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.no_plot:
        save_plot(out / f"probe_{label}_{ts}.png", records, label, args.host)

    print(f"  ✓ {label}：收 {summary['received']}/{summary['sent']} "
          f"丢 {summary['loss_rate']:.1%} ｜ RTT 中位 {summary['rtt_median']} ms / "
          f"P95 {summary['rtt_p95']} ms ｜ RSSI {summary['rssi_median']} dBm")
    if summary["loss_rate"] > 0.05 and not meta["inject"]:
        print("  ⚠ 丢包 >5% 且未注入：先排查供电/距离/防火墙，再决定这条要不要留")

    row = {
        "started_at": started.isoformat(timespec="seconds"), "label": label,
        "csv": csv_path.name, "json": json_path.name, "duration_s": duration,
        **{k: meta.get(k) for k in ("distance_m", "obstacle", "channel", "hotspot", "note")},
        "inject": json.dumps(meta["inject"], ensure_ascii=False) if meta["inject"] else "",
        **{k: summary.get(k) for k in ("sent", "received", "loss_rate", "rtt_median",
                                       "rtt_p95", "rssi_median")},
    }
    append_manifest(Path(args.out) / "manifest.csv", row)
    return row


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="按计划逐条采集链路 trace")
    ap.add_argument("--plan", default="plans/collect_plan.json")
    ap.add_argument("--host", required=True, help="ESP32-S3 的 IP（串口里看）")
    ap.add_argument("--udp-port", type=int, default=9000)
    ap.add_argument("--http-port", type=int, default=80)
    ap.add_argument("--out", default="traces")
    ap.add_argument("--hz", type=float, default=20.0)
    ap.add_argument("--timeout", type=float, default=0.2)
    ap.add_argument("--progress", type=float, default=5.0, help="实时打印间隔（秒），0=关闭")
    ap.add_argument("--only", default="", help="只采这些 label，逗号分隔")
    ap.add_argument("--limit", type=int, default=0, help="最多采几条（0=全部）")
    ap.add_argument("--yes", action="store_true", help="不等待回车（脚本化）")
    ap.add_argument("--no-plot", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不测量")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    plan_path = Path(args.plan)
    if not plan_path.is_file():
        raise SystemExit(f"计划文件不存在：{plan_path}")
    runs = load_plan(plan_path)
    if args.only:
        keep = {s.strip() for s in args.only.split(",") if s.strip()}
        runs = [r for r in runs if r["label"] in keep]
        if not runs:
            raise SystemExit(f"--only 里没有任何计划中的 label：{keep}")
    if args.limit:
        runs = runs[: args.limit]

    total_min = sum(float(r.get("duration_s", 300)) for r in runs) / 60.0
    print(f"计划：{plan_path} ｜ {len(runs)} 条 ｜ 总时长约 {total_min:.0f} 分钟 ｜ 目标 {args.host}")
    print(f"输出目录：{Path(args.out).resolve()}")
    if args.dry_run:
        for i, r in enumerate(runs, 1):
            inj = r.get("inject")
            print(f"  {i:2d}. {r['label']:16s} {r.get('duration_s', 300):>4}s "
                  f"距离 {r.get('distance_m')} m 遮挡 {r.get('obstacle', 'none'):<8s}"
                  f"{' [注入]' if inj else ''}")
        print("\n（--dry-run：没有开始测量）")
        return 0

    rows = []
    try:
        for i, run in enumerate(runs, 1):
            print(f"\n\n▶ 第 {i}/{len(runs)} 条")
            rows.append(run_one(run, args))
    except KeyboardInterrupt:
        print("\n已中止（已完成的部分已落盘并写入 manifest.csv）")

    manifest = Path(args.out) / "manifest.csv"
    print(f"\n\n采集结束：{len(rows)} 条已记录 → {manifest}")
    if rows:
        print(f"{'label':18s} {'sent':>6s} {'loss':>7s} {'RTT中位':>9s} {'P95':>8s} {'RSSI':>7s}")
        for r in rows:
            print(f"{r['label']:18s} {r['sent']:6d} {r['loss_rate']:7.1%} "
                  f"{str(r['rtt_median']):>9s} {str(r['rtt_p95']):>8s} {str(r['rssi_median']):>7s}")
        print("\n下一步：python ml/build_dataset.py --traces \"traces/probe_*.csv\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
