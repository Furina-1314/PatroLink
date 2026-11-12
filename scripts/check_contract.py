#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查「文档表格」与「机器可读契约」是否一致。

字段表最容易出的问题是：代码/JSON 改了，文档忘了改（或反过来）。
这个脚本把两边对一遍，任何一边漏了都报错。

用法：python scripts/check_contract.py
退出码：0 一致；1 不一致。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "schema" / "timeline.fields.json"
LOG_DOC = ROOT / "docs" / "log_contract.md"
ACT_DOC = ROOT / "docs" / "action_contract.md"


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    problems: list[str] = []
    schema = json.loads(SCHEMA.read_text(encoding="utf-8-sig"))
    fields = {f["name"] for f in schema["fields"]}
    version = schema["version"]

    # 1) 字段表 vs schema
    doc_text = LOG_DOC.read_text(encoding="utf-8-sig")
    doc_fields = set(re.findall(r"^\| `([a-z][a-z0-9_]*)` \|", doc_text, flags=re.M))
    missing_in_doc = sorted(fields - doc_fields)
    extra_in_doc = sorted(doc_fields - fields)
    if missing_in_doc:
        problems.append(f"schema 有、文档没有的字段：{missing_in_doc}")
    if extra_in_doc:
        problems.append(f"文档有、schema 没有的字段：{extra_in_doc}")

    # 2) 版本号一致
    m = re.search(r"log contract v(\d+\.\d+)", doc_text)
    if not m:
        problems.append("docs/log_contract.md 标题里找不到 'log contract vX.Y'")
    elif m.group(1) != version:
        problems.append(f"版本不一致：文档 v{m.group(1)}，schema v{version}")

    # 3) 动作表 vs act_name 枚举
    act_text = ACT_DOC.read_text(encoding="utf-8-sig")
    doc_actions = set(re.findall(r"^\| \d+ \| `([a-z_]+)` \|", act_text, flags=re.M))
    spec = {f["name"]: f for f in schema["fields"]}
    enum_actions = set(spec["act_name"]["enum"])
    if doc_actions - enum_actions:
        problems.append(f"动作文档有、schema 枚举没有：{sorted(doc_actions - enum_actions)}")
    if enum_actions - doc_actions:
        problems.append(f"schema 枚举有、动作文档没有：{sorted(enum_actions - doc_actions)}")

    # 4) 字段数量声明（changelog 里写的数）
    note = schema.get("changelog", [{}])[0].get("note", "")
    m = re.search(r"共 (\d+) 个字段", note)
    if m and int(m.group(1)) != len(fields):
        problems.append(f"changelog 声明 {m.group(1)} 个字段，实际 {len(fields)} 个")

    if problems:
        print("契约检查：FAIL")
        for p in problems:
            print("  - " + p)
        return 1

    print(f"契约检查：PASS ｜ 版本 v{version} ｜ 字段 {len(fields)} 个 ｜ 动作 {len(enum_actions)} 个")
    print(f"  文档表格字段 {len(doc_fields)} 个，与 schema 完全一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
