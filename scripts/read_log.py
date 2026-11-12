#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读取 timeline.jsonl 并打印人看得懂的摘要。

有 pandas 时读成 DataFrame（面板/实验里也这么用）；没有 pandas 也能跑。

用法：
    python scripts/read_log.py logs/sample_frames.jsonl
    python scripts/read_log.py logs/sample_frames.jsonl --head 8
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

KEY_COLS = ["frame_id", "t_frame_ms", "exec_t_total_ms", "confidence_calibrated",
            "mode", "resolution", "link_rtt_ms", "link_loss_rate", "act_name",
            "exec_bytes_up", "exec_success", "rw_total"]


def read_records(path: Path) -> list[dict]:
    records = []
    with path.open(encoding="utf-8-sig") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{lineno} JSON 解析失败：{exc.msg}")
    return records


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="读取并摘要 timeline.jsonl")
    ap.add_argument("path", help="JSONL 文件路径")
    ap.add_argument("--head", type=int, default=6, help="打印前 N 个 tick（默认 6）")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    path = Path(args.path)
    if not path.is_file():
        raise SystemExit(f"文件不存在：{path}")

    try:
        import pandas as pd
        df = pd.read_json(path, lines=True, encoding="utf-8")
        ticks = df[df.get("event") == "tick"] if "event" in df.columns else df
        print(f"文件 {path}：{len(df)} 行，其中 tick {len(ticks)} 行")
        print("列：", ", ".join(df.columns))
        cols = [c for c in KEY_COLS if c in ticks.columns]
        print(f"\n前 {args.head} 个 tick：")
        print(ticks[cols].head(args.head).to_string(index=False))
        if "exec_t_total_ms" in ticks.columns and len(ticks):
            q = ticks["exec_t_total_ms"].quantile([0.5, 0.95]).round(1).to_dict()
            print(f"\n端到端时延：P50 {q.get(0.5)} ms ｜ P95 {q.get(0.95)} ms")
            print(f"任务成功率：{ticks['exec_success'].mean():.1%}")
            print(f"总上行流量：{int(ticks['exec_bytes_up'].sum()) / 1024:.1f} KB")
        if "act_name" in ticks.columns:
            print("\n动作分布：", ticks["act_name"].value_counts().to_dict())
        return 0
    except ImportError:
        pass

    # 纯标准库兜底
    records = read_records(path)
    ticks = [r for r in records if r.get("event") == "tick"]
    print(f"文件 {path}：{len(records)} 行，其中 tick {len(ticks)} 行（未安装 pandas，使用标准库）")
    print("提示：pip install -r requirements.txt 可启用 DataFrame 视图\n")
    for rec in ticks[: args.head]:
        print("  " + " | ".join(f"{k}={rec.get(k)}" for k in KEY_COLS if k in rec))
    return 0


if __name__ == "__main__":
    sys.exit(main())
