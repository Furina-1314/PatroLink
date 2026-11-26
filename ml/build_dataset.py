#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把探针 CSV 变成"监督学习数据集"：滑窗特征 + 未来窗口标签。

定义（也是报告里必须写清的口径）：
    特征窗口  W = 2.0 s（过去）      —— 只用过去，绝不看未来
    预测窗口  H = 1.0 s（未来）      —— 标签只看这一段
    滑窗步长  S = 0.2 s
    标签：未来 H 内的 RTT P95 ≥ --rtt-thresh  或  丢包率 ≥ --loss-thresh  → 1（将恶化）

用法：
    python ml/build_dataset.py --traces traces/demo/*.csv --out ml/artifacts/dataset.csv
    python ml/build_dataset.py --traces "traces/probe_*.csv" --window 2 --horizon 1
"""
from __future__ import annotations

import argparse
import csv
import glob
import sys
from pathlib import Path

import numpy as np

FEATURES = [
    "rtt_med", "rtt_p95", "rtt_std", "rtt_min", "rtt_max", "rtt_slope",
    "loss_rate", "jitter_med", "rssi_med", "rssi_std", "rssi_slope",
    "valid_ratio", "rtt_delta",
]


def load_trace(path: Path):
    """返回 dict of numpy arrays（按时间排序）。"""
    ts, rtt, rssi, lost = [], [], [], []
    with path.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            ts.append(float(row["wall_ms"]) / 1000.0)
            rtt.append(float(row["rtt_ms"]) if row["rtt_ms"] not in ("", None) else np.nan)
            rssi.append(float(row["rssi_dbm"]) if row.get("rssi_dbm") not in ("", None) else np.nan)
            lost.append(int(row["lost"]))
    order = np.argsort(ts)
    return {
        "name": path.name,
        "t": np.array(ts)[order],
        "rtt": np.array(rtt, dtype=float)[order],
        "rssi": np.array(rssi, dtype=float)[order],
        "lost": np.array(lost, dtype=int)[order],
    }


def stats(x: np.ndarray):
    """忽略 NaN 的统计量。"""
    v = x[~np.isnan(x)]
    if v.size == 0:
        return dict(med=np.nan, p95=np.nan, std=np.nan, mn=np.nan, mx=np.nan, slope=np.nan)
    return dict(
        med=float(np.median(v)),
        p95=float(np.percentile(v, 95)),
        std=float(np.std(v)),
        mn=float(np.min(v)),
        mx=float(np.max(v)),
        slope=float(np.polyfit(np.arange(v.size), v, 1)[0]) if v.size > 2 else 0.0,
    )


def compute_rows(trace, window_s: float, horizon_s: float, step_s: float,
                 rtt_thresh: float, loss_thresh: float):
    """对一条 trace 计算所有滑窗样本（面板也复用这个函数，保证口径一致）。"""
    t, rtt, rssi, lost = trace["t"], trace["rtt"], trace["rssi"], trace["lost"]
    if len(t) < 10:
        return []
    dt = float(np.median(np.diff(t))) if len(t) > 1 else 0.05
    w = max(3, int(round(window_s / dt)))
    h = max(1, int(round(horizon_s / dt)))
    s = max(1, int(round(step_s / dt)))

    rows = []
    for i in range(w - 1, len(t) - h, s):
        past = slice(i - w + 1, i + 1)
        future = slice(i + 1, i + 1 + h)
        st = stats(rtt[past])
        rs = stats(rssi[past])
        loss_rate = float(np.mean(lost[past]))
        diffs = np.abs(np.diff(rtt[past])) if w > 2 else np.array([np.nan])
        jitter = float(np.nanmedian(diffs)) if np.any(~np.isnan(diffs)) else np.nan
        valid = float(np.mean(~np.isnan(rtt[past])))
        delta = float(st["med"] - np.nanmedian(rtt[past][: max(1, w // 4)])) \
            if np.any(~np.isnan(rtt[past])) else np.nan

        f_rtt = rtt[future]
        fut_p95 = float(np.nanpercentile(f_rtt, 95)) if np.any(~np.isnan(f_rtt)) else np.nan
        fut_loss = float(np.mean(lost[future]))
        degraded = int((not np.isnan(fut_p95) and fut_p95 >= rtt_thresh) or fut_loss >= loss_thresh)

        rows.append({
            "trace": trace["name"],
            "t_s": round(float(t[i]), 3),
            "rtt_med": st["med"], "rtt_p95": st["p95"], "rtt_std": st["std"],
            "rtt_min": st["mn"], "rtt_max": st["mx"], "rtt_slope": st["slope"],
            "loss_rate": loss_rate, "jitter_med": jitter,
            "rssi_med": rs["med"], "rssi_std": rs["std"], "rssi_slope": rs["slope"],
            "valid_ratio": valid, "rtt_delta": delta,
            "label": degraded,
        })
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="滑窗造数据集（特征=过去，标签=未来）")
    ap.add_argument("--traces", nargs="+", default=["traces/demo/*.csv"],
                    help="CSV 文件或 glob（支持多个）")
    ap.add_argument("--out", default="ml/artifacts/dataset.csv")
    ap.add_argument("--window", type=float, default=2.0, help="过去特征窗口（秒）")
    ap.add_argument("--horizon", type=float, default=1.0, help="未来预测窗口（秒）")
    ap.add_argument("--step", type=float, default=0.2, help="滑窗步长（秒）")
    ap.add_argument("--rtt-thresh", type=float, default=80.0,
                    help="未来窗口 RTT P95 超过它就算恶化（毫秒）")
    ap.add_argument("--loss-thresh", type=float, default=0.03,
                    help="未来窗口丢包率超过它就算恶化")
    args = ap.parse_args(argv)

    files = []
    for pattern in args.traces:
        files.extend(sorted(glob.glob(pattern)))
    files = [Path(f) for f in files if Path(f).is_file()]
    if not files:
        raise SystemExit(f"没找到 trace：{args.traces}")

    all_rows = []
    for p in files:
        tr = load_trace(p)
        rows = compute_rows(tr, args.window, args.horizon, args.step,
                           args.rtt_thresh, args.loss_thresh)
        pos = sum(r["label"] for r in rows)
        print(f"  {p.name}: {len(rows)} 样本，正例 {pos} ({pos / max(1, len(rows)):.1%})")
        all_rows.extend(rows)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cols = ["trace", "t_s"] + FEATURES + ["label"]
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in all_rows:
            w.writerow(r)

    total_pos = sum(r["label"] for r in all_rows)
    print(f"\n总计 {len(all_rows)} 样本，正例 {total_pos} ({total_pos / max(1, len(all_rows)):.1%})")
    print(f"已保存：{out}")
    if total_pos == 0:
        print("⚠️ 没有正例：把 --rtt-thresh 调低，或让 trace 里包含恶化区间", file=sys.stderr)
    return 0


# 兼容旧名字（外部脚本可能还在用）
build_trace = compute_rows


if __name__ == "__main__":
    raise SystemExit(main())
