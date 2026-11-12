#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按 schema/timeline.fields.json 校验 timeline.jsonl。

零第三方依赖，Python 3.8+ 即可运行。

用法：
    python scripts/validate_log.py logs/sample_frames.jsonl
    python scripts/validate_log.py logs/*.jsonl --strict
    python scripts/validate_log.py logs/sample_frames.jsonl --summary

退出码：0 通过；1 有错误（--strict 时警告也算错）。
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCHEMA = ROOT / "schema" / "timeline.fields.json"


class Issue:
    __slots__ = ("level", "line", "field", "msg")

    def __init__(self, level: str, line: int, field: str, msg: str):
        self.level, self.line, self.field, self.msg = level, line, field, msg

    def __str__(self) -> str:
        loc = f"L{self.line}" if self.line else "文件"
        fld = f"[{self.field}] " if self.field else ""
        mark = {"error": "ERROR", "warn": "WARN "}[self.level]
        return f"{loc} {mark} {fld}{self.msg}"


def load_schema(path: Path) -> dict:
    with path.open(encoding="utf-8-sig") as f:
        return json.load(f)


def type_ok(value, spec: dict) -> bool:
    if value is None:
        return bool(spec.get("nullable"))
    t = spec.get("type")
    if t == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "string":
        return isinstance(value, str)
    if t == "bool":
        return isinstance(value, bool)
    if t == "object":
        return isinstance(value, dict)
    return True


def parse_line(raw: str):
    """返回 (obj, error_msg)。禁止重复键，重复即报错。"""
    dupes = []

    def hook(pairs):
        seen = {}
        for k, v in pairs:
            if k in seen:
                dupes.append(k)
            seen[k] = v
        return seen

    try:
        obj = json.loads(raw, object_pairs_hook=hook)
    except json.JSONDecodeError as exc:
        return None, f"JSON 解析失败：{exc.msg}（第 {exc.colno} 列）"
    if dupes:
        return obj, f"同一行出现重复字段：{', '.join(sorted(set(dupes)))}"
    return obj, None


def validate_file(path: Path, schema: dict, strict: bool):
    fields = {f["name"]: f for f in schema["fields"]}
    required_by_event = schema["required_by_event"]
    version = schema["version"]
    issues: list[Issue] = []
    stats = {
        "lines": 0, "events": Counter(), "actions": Counter(), "modes": Counter(),
        "zones": Counter(), "latency": [], "success": 0, "tick": 0,
    }
    seen_start: set[str] = set()

    with path.open(encoding="utf-8-sig") as f:
        for lineno, raw in enumerate(f, start=1):
            raw = raw.strip()
            if not raw:
                continue
            stats["lines"] += 1
            obj, perr = parse_line(raw)
            if perr:
                issues.append(Issue("error", lineno, "", perr))
                continue
            if not isinstance(obj, dict):
                issues.append(Issue("error", lineno, "", "一行必须是一个 JSON 对象"))
                continue

            event = obj.get("event")
            stats["events"][event] += 1
            if event not in required_by_event:
                issues.append(Issue("error", lineno, "event",
                                    f"{event!r} 不在允许的事件类型 {list(required_by_event)} 里"))
                continue

            ver = obj.get("schema_version")
            if ver != version:
                issues.append(Issue("error", lineno, "schema_version",
                                    f"版本 {ver!r} 与契约版本 {version!r} 不一致"))

            run_id = obj.get("run_id")
            if event == "episode_start" and isinstance(run_id, str):
                seen_start.add(run_id)
            if event == "tick" and isinstance(run_id, str) and run_id not in seen_start:
                issues.append(Issue("warn", lineno, "run_id",
                                    f"{run_id} 的 tick 出现在 episode_start 之前"))

            # 1) 必填
            for name in required_by_event[event]:
                if name not in obj or obj[name] is None:
                    issues.append(Issue("error", lineno, name, "缺少必填字段"))

            # 2) 已登记字段的类型 / 取值 / 范围
            for name, value in obj.items():
                spec = fields.get(name)
                if spec is None:
                    issues.append(Issue("warn", lineno, name, "未在契约中登记的字段"))
                    continue
                if value is None:
                    if not spec.get("nullable"):
                        issues.append(Issue("error", lineno, name,
                                            "不允许为 null（契约未标 nullable）"))
                    continue
                if not type_ok(value, spec):
                    issues.append(Issue("error", lineno, name,
                                        f"类型错误：期望 {spec['type']}，实际 {type(value).__name__}"))
                    continue
                if "const" in spec and value != spec["const"]:
                    issues.append(Issue("error", lineno, name, f"应为常量 {spec['const']!r}"))
                if "enum" in spec and value not in spec["enum"]:
                    issues.append(Issue("error", lineno, name,
                                        f"取值 {value!r} 不在枚举 {spec['enum']} 中"))
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    if "min" in spec and value < spec["min"]:
                        issues.append(Issue("error", lineno, name, f"{value} 小于下限 {spec['min']}"))
                    if "max" in spec and value > spec["max"]:
                        issues.append(Issue("error", lineno, name, f"{value} 大于上限 {spec['max']}"))
                if isinstance(value, str) and "pattern" in spec:
                    if not re.fullmatch(spec["pattern"], value):
                        issues.append(Issue("error", lineno, name,
                                            f"{value!r} 不匹配 {spec['pattern']}"))

            # 3) 跨字段一致性（tick）
            if event == "tick":
                stats["tick"] += 1
                tf, tm = obj.get("t_frame_ms"), obj.get("t_ms")
                total = obj.get("exec_t_total_ms")
                if isinstance(tf, int) and isinstance(tm, int):
                    if tm < tf:
                        issues.append(Issue("error", lineno, "t_ms", "决策时刻早于帧采集时刻"))
                    elif isinstance(total, (int, float)):
                        delta = (tm - tf) - total
                        if abs(delta) > 1.5:
                            issues.append(Issue(
                                "warn", lineno, "exec_t_total_ms",
                                f"与 t_ms−t_frame_ms={(tm - tf)} ms 相差 {delta:+.1f} ms"))
                comps = ["rw_task", "rw_latency", "rw_bytes", "rw_conf_margin", "rw_switch"]
                got = [obj[c] for c in comps if isinstance(obj.get(c), (int, float))]
                if len(got) == len(comps) and isinstance(obj.get("rw_total"), (int, float)):
                    s = sum(got)
                    if abs(s - obj["rw_total"]) > 0.06:
                        issues.append(Issue("warn", lineno, "rw_total",
                                            f"分量和={s:.3f}，与 rw_total={obj['rw_total']:.3f} 不一致"))
                if isinstance(total, (int, float)):
                    stats["latency"].append(float(total))
                stats["success"] += int(obj.get("exec_success") or 0)
                if obj.get("act_name"):
                    stats["actions"][obj["act_name"]] += 1
                if obj.get("mode"):
                    stats["modes"][obj["mode"]] += 1
                if obj.get("link_zone"):
                    stats["zones"][obj["link_zone"]] += 1

    return issues, stats


def pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def print_summary(name: str, stats: dict) -> None:
    print(f"\n── 摘要 {name} ──")
    print(f"  行数 {stats['lines']} ｜ tick {stats['tick']} ｜ 事件 {dict(stats['events'])}")
    if stats["tick"]:
        print(f"  任务成功率 {stats['success'] / stats['tick']:.1%}"
              f" ｜ 时延 P50 {pct(stats['latency'], .5):.1f} ms"
              f" ｜ P95 {pct(stats['latency'], .95):.1f} ms")
        print(f"  动作分布 {dict(stats['actions'])}")
        print(f"  推理位置 {dict(stats['modes'])} ｜ 链路场景 {dict(stats['zones'])}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="校验 PatroLink timeline.jsonl")
    ap.add_argument("paths", nargs="+", help="JSONL 文件或 glob，如 logs/*.jsonl")
    ap.add_argument("--schema", default=str(DEFAULT_SCHEMA), help="字段契约 JSON")
    ap.add_argument("--strict", action="store_true", help="警告也视为失败")
    ap.add_argument("--summary", action="store_true", help="打印统计摘要")
    ap.add_argument("--quiet", action="store_true", help="只输出错误，不打印每条警告")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    schema = load_schema(Path(args.schema))
    files: list[Path] = []
    for pattern in args.paths:
        matched = sorted(Path().glob(pattern)) if any(c in pattern for c in "*?[") else [Path(pattern)]
        files.extend(p for p in matched if p.is_file())
    if not files:
        print("未找到待校验文件：", args.paths)
        return 1

    total_err = total_warn = 0
    for path in files:
        issues, stats = validate_file(path, schema, args.strict)
        errs = [i for i in issues if i.level == "error"]
        warns = [i for i in issues if i.level == "warn"]
        shown = errs + ([] if args.quiet else warns)
        if shown:
            print(f"\n{path}：")
            for issue in shown:
                print("  " + str(issue))
        status = "PASS" if not errs and (not warns or not args.strict) else "FAIL"
        print(f"{path}: {status} ｜ 错误 {len(errs)} ｜ 警告 {len(warns)} ｜ 行 {stats['lines']}")
        if args.summary:
            print_summary(path.name, stats)
        total_err += len(errs)
        total_warn += len(warns)

    print(f"\n合计：错误 {total_err} ｜ 警告 {total_warn}")
    return 0 if total_err == 0 and (not args.strict or total_warn == 0) else 1


if __name__ == "__main__":
    sys.exit(main())

