#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""面板冒烟测试（不需要浏览器）：用 Streamlit 的 AppTest 把四个页签跑一遍。

CI 里跑；本地也可以先跑一次确认面板没写崩：
    python tools/panel_smoke.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    app = ROOT / "panel" / "app.py"
    if not app.is_file():
        print("找不到 panel/app.py")
        return 1
    at = AppTest.from_file(str(app), default_timeout=120)
    at.run()
    if at.exception:
        print("面板抛异常：")
        for e in at.exception:
            print("  -", e.value)
        return 1
    print(f"面板 OK：{len(at.tabs)} 个页签 {[t.label for t in at.tabs]}，"
          f"{len(at.metric)} 个指标，{len(at.dataframe)} 个表格")
    return 0


if __name__ == "__main__":
    sys.exit(main())
