#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成"演示用"链路 trace（与 pc/probe_link.py 的 CSV 格式完全一致）。

用途：在还没采到真实数据时，把"造数据 → 训练 → 评估"整条链路先跑通。
注意：这些是**合成数据**，任何指标都不能当实验证据；真数据请用探针采。

用法：
    python ml/make_demo_traces.py                 # 生成 good/degrade/oscillate 三条
    python ml/make_demo_traces.py --out traces/demo --seconds 120
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import random
from datetime import datetime
from pathlib import Path


def write_trace(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["wall_ms", "seq", "rtt_ms", "rssi_dbm", "lost"])
        w.writerows(rows)


def make(kind: str, seconds: float, hz: float, seed: int):
    rng = random.Random(seed)
    n = int(seconds * hz)
    dt = 1000.0 / hz
    t0 = int(datetime.now().timestamp() * 1000)
    rows = []
    for i in range(n):
        t = i / hz
        if kind.startswith("good"):
            rtt, loss, rssi = 22 + rng.gauss(0, 1.5), 0.002, -48 + rng.gauss(0, 1.0)
        elif kind == "degrade":
            # 两个恶化事件，方便按时间切分时两边都有正例
            #   0-60 好；60-90 恶化；90-110 维持；110-140 恢复；
            #   140-200 好；200-230 恶化；230-260 维持；260-300 恢复
            if t < 60:
                base, loss, rssi = 22, 0.002, -48
            elif t < 90:
                k = (t - 60) / 30
                base, loss, rssi = 22 + 130 * k, 0.002 + 0.07 * k, -48 - 28 * k
            elif t < 110:
                base, loss, rssi = 152, 0.072, -76
            elif t < 140:
                k = min(1.0, (t - 110) / 30)
                base, loss, rssi = 152 - 130 * k, 0.072 - 0.07 * k, -76 + 28 * k
            elif t < 200:
                base, loss, rssi = 22, 0.002, -48
            elif t < 230:
                k = (t - 200) / 30
                base, loss, rssi = 22 + 130 * k, 0.002 + 0.07 * k, -48 - 28 * k
            elif t < 260:
                base, loss, rssi = 152, 0.072, -76
            else:
                k = min(1.0, (t - 260) / 40)
                base, loss, rssi = 152 - 130 * k, 0.072 - 0.07 * k, -76 + 28 * k
            rtt = base + rng.gauss(0, 4 + 0.06 * base)
        elif kind == "oscillate":
            k = 0.5 + 0.5 * math.sin(2 * math.pi * t / 20.0)
            rtt = 25 + 100 * k + rng.gauss(0, 3)
            loss = 0.002 + 0.04 * k
            rssi = -48 - 25 * k + rng.gauss(0, 1.2)
        else:
            raise SystemExit(f"unknown kind {kind}")

        lost = 1 if rng.random() < loss else 0
        # 丢包时按时延超时记（probe 的输出里 rtt 为空）
        rows.append([t0 + int(t * 1000), i + 1,
                     "" if lost else f"{max(5.0, rtt):.3f}",
                     f"{rssi:.1f}", lost])
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成演示用链路 trace（合成数据）")
    ap.add_argument("--out", default="traces/demo")
    ap.add_argument("--seconds", type=float, default=300.0)
    ap.add_argument("--hz", type=float, default=20.0)
    args = ap.parse_args(argv)

    out = Path(args.out)
    kinds = [("good", "good_1"), ("good", "good_2"), ("degrade", "degrade"),
             ("oscillate", "oscillate")]
    for i, (kind, name) in enumerate(kinds):
        rows = make(kind, args.seconds, args.hz, seed=100 + 37 * i)
        p = out / f"{name}.csv"
        write_trace(p, rows)
        print(f"  {p}  {len(rows)} 行  ({kind}, {args.seconds:g}s @ {args.hz:g}Hz)")
    print("\n提醒：这是合成数据，只能用来跑通流程，不能写进报告当证据。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
