# PatroLink

面向室内巡检场景的链路感知传输与推理调度原型系统。2026 数智融和大赛参赛项目。

## 1. 项目简介

巡检机器人的视觉任务依赖上行链路：一旦链路恶化，帧率、分辨率与推理位置都需要临时调整。常见做法是固定配置、等到超时或丢包后再补救（反应式），或者只在仿真环境中验证（不可复核）。

本项目在真实 WiFi 上构建一条可复现的链路实验装置，包括：

1. 用 ESP32-S3 作为链路探针节点，按 20 Hz 采集 RTT、丢包与 RSSI；
2. 用过去 2 s 的链路指标预测未来 1 s 是否恶化（监督学习二分类）；
3. 由调度器根据预测结果提前调整传输与推理策略（分辨率、帧率、上传与否、端侧/边缘）；
4. 以统一的时间线日志记录决策过程，并通过面板与对照实验评估效果。

结论边界：链路为"类 URLLC 约束"的 WiFi 实测与可控注入，不宣称 5G 专网；视觉模型使用现成轻量模型，不训练、不比较检测精度。

## 2. 当前进度

| 模块 | 状态 | 位置 |
|---|---|---|
| 日志与动作契约 v0.1 | 完成 | `docs/log-contract.md`、`docs/action-contract.md`、`schema/timeline.fields.json` |
| ESP32-S3 链路探针 | 固件编译通过（`esp32:esp32:esp32s3`，core 3.3.10），待实机采集 | `firmware/esp32s3_probe/`、`pc/probe_link.py` |
| 采集编排 | 完成（含 manifest 记账） | `pc/collect_dataset.py`、`plans/` |
| 链路风险预测器 | 代码完成（合成数据验证），待实测数据重训 | `ml/` |
| 可视化面板 | 完成（采集回放 / 模型评估 / 数据总览） | `panel/app.py` |
| 视觉推理管线 | 未开始 | — |
| 调度闭环与对照实验 | 未开始 | — |

## 3. 快速开始

```powershell
# 环境（Python 3.10+；核心校验脚本零依赖，其余按需安装）
pip install -r requirements.txt

# 1) 契约校验（不需要硬件）
python scripts/check_contract.py
python scripts/validate_log.py logs/sample_frames.jsonl --strict --summary

# 2) 链路采集：无硬件时先自检
python tools/fake_esp32_probe.py                # 终端 A
python pc/probe_link.py --host 127.0.0.1 --udp-port 9000 --http-port 8080 --duration 5 --label selftest

#    真机：烧录固件后（见 docs/experiment-protocol.md）
python pc/collect_dataset.py --host 192.168.137.2 --dry-run
python pc/collect_dataset.py --host 192.168.137.2

# 3) 训练与评估
python ml/make_demo_traces.py                     # 合成数据，仅用于跑通流程
python ml/build_dataset.py --traces "traces/demo/*.csv"
python ml/train_predictor.py --data ml/artifacts/dataset.csv --split trace --out ml/artifacts/trace

# 4) 可视化面板
streamlit run panel/app.py                        # http://localhost:8501
python tools/panel_smoke.py                       # 无浏览器冒烟测试
```

## 4. 仓库结构

```
PatroLink/
├─ docs/                      # 文档（契约、实验、模型、面板、答辩）
├─ firmware/esp32s3_probe/    # ESP32-S3 链路探针固件
├─ pc/                        # 采集与测量（probe_link / collect_dataset）
├─ plans/                     # 采集计划（默认 9 条 / CI 迷你计划）
├─ ml/                        # 数据集构建、预测器训练与评估
├─ panel/                     # Streamlit 可视化面板
├─ scripts/                   # 契约校验、日志读取、一致性检查
├─ schema/                    # 机器可读字段契约
├─ logs/                      # 样例时间线日志
├─ tools/                     # 假节点、面板冒烟测试
├─ traces/                    # 采集产物（默认不入库）
└─ .github/workflows/ci.yml   # 持续集成
```

## 5. 实验与数据规范

- **契约优先**：字段改动需同步 `schema/timeline.fields.json`、`docs/log-contract.md`、`logs/sample_frames.jsonl`，升版本号，并运行 `scripts/check_contract.py` 与 `scripts/validate_log.py --strict`。
- **数据来源优先级**：真实 WiFi 实测为主；可控注入（Clumsy 延迟丢包）用于扩增；纯仿真仅用于流程验证与极端场景测试，不作为训练与测试证据。
- **划分原则**：主指标采用留一 trace（LOTO）；测试集必须包含全程良好的 trace；注入数据只进训练集。
- **分支与提交**：功能改动走 feature 分支与 PR；提交前本地运行 CI 同等检查（`docs/` 校验、日志校验、面板冒烟）。CI 配置见 `.github/workflows/ci.yml`。

## 6. 文档索引

| 文档 | 内容 |
|---|---|
| [docs/log-contract.md](docs/log-contract.md) | 共享时间线日志字段表（47 字段，v0.1） |
| [docs/action-contract.md](docs/action-contract.md) | 调度动作契约（7 个动作、reason 标签） |
| [docs/experiment-protocol.md](docs/experiment-protocol.md) | 硬件与采集设置、9 条采集计划、注入方法、故障排查 |
| [docs/predictor.md](docs/predictor.md) | 预测器：标签、特征、划分、基线、评估与接入 |
| [docs/panel.md](docs/panel.md) | 可视化面板：启动、页签、数据源 |
| [docs/defense.md](docs/defense.md) | 答辩要点：与现有方案对比、创新点、预设问答 |

## 7. 环境与依赖

- Python 3.10+；`scripts/` 下三个脚本仅依赖标准库；
- 其余依赖见 `requirements.txt`（pandas、numpy、scikit-learn、joblib、matplotlib、streamlit、plotly）；
- 训练与推理均可在普通笔记本 CPU 上完成，无需 GPU；
- 文件统一 UTF-8（无 BOM）。

## 8. 阶段计划

| 周次 | 内容 | 验收物 |
|---|---|---|
| W1 | ESP32 探针联调、真实链路采集 | 9 条 trace + manifest |
| W2 | 日志字段 v0.2（接入实测链路字段）、视觉管线骨架 | 一回合日志通过校验 |
| W3 | 预测器改用实测数据训练与评估 | AUC/提前量/误报报告 |
| W4 | 调度器（规则 + 一步前瞻 + 滞回） | 三策略可运行 |
| W5 | 面板接入时间线日志 | 决策可解释的回放 |
| W6 | 对照实验（40 回合）与消融 | 主结果图与指标表 |
| W7 | 报告与答辩材料 | PPT、演示视频 |
| W8 | 打包、复现检查、答辩演练 | 一键复现 |
