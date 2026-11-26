# PatroLink

> **巡检（Patrol）+ 链路（Link）闭环**：通信受限条件下的具身视觉推理管线——可观测、可评价、可调度。
> 2026 数智融和大赛 · 老生组 · 2 人团队（本科大二）

---

## 现在到哪一步了

| 步骤 | 内容 | 状态 |
|---|---|---|
| **Step 1** | **共享日志字段表 + 动作契约（v0.1）** | ✅ 完成（PR #1） |
| **Step 2a** | **ESP32-S3 链路探针：真实 WiFi 的 RTT / 丢包 / RSSI / 吞吐** | ✅ 代码就绪（固件已编译通过），等实机采数 |
| Step 2b | 视觉黑箱推理管线跑通，按契约吐日志 | ⬜ 下一步 |
| Step 3 | 置信度校准（温度缩放） | ⬜ |
| **Step 4** | **链路风险预测器（监督学习：过去 2 s → 未来 1 s 是否恶化）** | ✅ 代码就绪（合成数据验证），等实测 trace |
| Step 5 | 可观测面板（时间线 / 瓶颈归因 / 决策解释） | ⬜ |
| Step 6 | 自适应调度 + 对照实验 | ⬜ |

---

## 3 分钟验证（不需要装任何第三方库）

```bash
# 1) 校验样例日志是否符合契约（必填/类型/枚举/时延一致性）
python scripts/validate_log.py logs/sample_frames.jsonl --summary

# 2) 检查文档字段表与机器可读契约是否一致
python scripts/check_contract.py

# 3) 把日志读成人能看的摘要（装了 pandas 会输出 DataFrame）
python scripts/read_log.py logs/sample_frames.jsonl
```

预期：前两个都打印 `PASS`，第三个打印 8 个 tick、P50/P95 时延和动作分布。

---

## 仓库结构

```
PatroLink/
├─ firmware/esp32s3_probe/     # ★ ESP32-S3 探针固件（UDP 回显 + HTTP /rssi /sink /echo）
├─ pc/probe_link.py            # ★ PC 侧探针：四时间戳算 RTT，出 CSV/JSON/PNG
├─ pc/collect_dataset.py       # ★ 按计划逐条采集（自动记账到 traces/manifest.csv）
├─ plans/collect_plan.json     # 采集计划：9 条（good/wall/person/far/outage/inject）
├─ ml/                         # ★ 链路风险预测器：造样本 → 训练 → 评估 → 出图
│  ├─ make_demo_traces.py      # 合成演示 trace（没有真数据时先跑通流程）
│  ├─ build_dataset.py         # 滑窗造特征 + 未来窗口标签
│  └─ train_predictor.py       # 基线 / 逻辑回归 / 梯度提升 + ROC/PR/时间序列图
├─ tools/fake_esp32_probe.py   # 无硬件时模拟探针节点（UDP+HTTP）
├─ docs/
│  ├─ log_contract.md          # 共享时间线日志字段表（v0.1，主交付物）
│  ├─ action_contract.md       # 调度动作契约（7 个动作 + reason 标签）
│  ├─ collect_playbook.md      # ★ 明天照着做：谁做什么、怎么设、怎么采、怎么观察
│  ├─ probe_guide.md           # 探针烧录 / 测量 / 故障排查（按这个做 W1 验收）
│  └─ dataset_protocol.md      # 数据从哪来：实测为主 / 可控注入扩增 / 仿真只测代码
├─ schema/timeline.fields.json # 机器可读字段契约（校验脚本唯一数据源）
├─ logs/sample_frames.jsonl    # 手写样例：1 回合 8 个 tick
├─ scripts/
│  ├─ validate_log.py          # 按契约校验任意 timeline.jsonl
│  ├─ read_log.py              # 读日志 → 摘要（pandas 可选）
│  └─ check_contract.py        # 文档表格 vs schema 一致性检查
├─ traces/                     # 探针产物（CSV/JSON/PNG，不进 git）
├─ requirements.txt            # pandas / matplotlib（可选）
└─ .gitignore
```

---

## 两条必须守住的规则

1. **只认一条 `timeline.jsonl`**：视觉、链路、调度、奖励四个模块都往它写，面板和指标只从它读。禁止另开 CSV／另建 dict 直写——这是两个人协作最容易散架的地方。
2. **改字段 = 改三处 + 升版本**：`schema/timeline.fields.json`（机器可读）→ `docs/log_contract.md`（人读表格）→ `logs/sample_frames.jsonl`（样例），然后跑 `python scripts/check_contract.py` 和 `python scripts/validate_log.py logs/sample_frames.jsonl --strict`。任何一处漏了都会报错。

---

## 契约要点（详见 `docs/log_contract.md`）

- **时间基准**：Unix 毫秒时间戳（int, UTC）。帧采集写 `t_frame_ms`，决策完成写 `t_ms`，**端到端时延 = `t_ms − t_frame_ms`**，没有第二个口径。
- **一行 = 一个决策时刻**（`event:"tick"`）：这一时刻的视觉、链路、动作、执行、奖励写在同一行里；`episode_start` / `episode_end` 只写回合边界。
- **47 个字段**，其中 tick 必填 22 个；可选字段用"建议/可选"标注，第 2–4 周逐步补齐。
- **单位写死在表里**：`_ms` 就是毫秒，`_ratio` / `loss_rate` 是 0–1 小数（`0.02 = 2%`），`_mbps` 是 Mbps。
- **动作取值** 7 个：`keep` / `downscale` / `lower_fps` / `crop_roi` / `switch_edge` / `switch_device` / `skip_upload`；动作作用于**下一帧**，下一帧日志必须反映效果。

---

## 链路探针（Step 2a，W1 主线）

```powershell
# 1) 烧固件：改 firmware/esp32s3_probe/esp32s3_probe.ino 里的 SSID/密码，
#    Arduino IDE 选 ESP32S3 Dev Module（USB CDC On Boot = Enabled）→ 串口看 IP
# 2) 没有硬件也能先自测：
python tools\fake_esp32_probe.py --pattern degrade        # 终端 A
python pc\probe_link.py --host 127.0.0.1 --udp-port 9000 --http-port 8080 --duration 10 --label fake  # 终端 B
# 3) 真实测量（IP 换成串口里看到的）：
python pc\probe_link.py --host 192.168.137.2 --duration 30 --label desk
```

- 原理：UDP 四时间戳，`RTT = (t3−t0) − (t2−t1)`，**不需要两端对时**；
- 产物：`traces/probe_<label>_<ts>.{csv,json,png}`（RTT / 丢包 / RSSI / 吞吐）；
- **W1 验收**：三个位置（桌边 / 隔墙 / 楼道）的 RTT 中位数差 ≥ 10 ms 或 RSSI 差 ≥ 5 dBm；
- 完整步骤与故障排查见 [`docs/probe_guide.md`](docs/probe_guide.md)。

---

## 链路风险预测器（Step 4）

```powershell
pip install -r requirements.txt          # scikit-learn / joblib / matplotlib
python ml\make_demo_traces.py            # 合成演示数据（真实数据用探针采，见 docs/probe_guide.md）
python ml\build_dataset.py --traces "traces/demo/*.csv"
python ml\train_predictor.py --data ml\artifacts\dataset.csv --split trace --out ml\artifacts\trace
```

- 标签：未来 1 s 内 RTT P95 ≥ 80 ms 或丢包 ≥ 3% → 恶化；特征只用过去 2 s（13 维）；
- 基线：`persistence`（已经坏了才说坏）、`rule`（人工阈值），**必须先打败它们**；
- 划分：`--split time`（同场景往后）与 `--split trace`（留一条 trace 不训练，测跨场景）；
- 产物：`metrics.json`、`model.joblib`、ROC/PR 图、**预测概率 vs 真实恶化**时间序列图；
- 完整方法、结果与踩坑清单见 [`docs/predictor_guide.md`](docs/predictor_guide.md)。
- **数据从哪来**：实测为主、可控注入扩增、仿真只测代码——见 [`docs/dataset_protocol.md`](docs/dataset_protocol.md)（含 12 条 trace 的采集矩阵与元数据字段）。

---

## 下一步（Step 2b：视觉管线按契约吐日志）

1. 手机拍 1 分钟巡检视频（走廊/实验室），按固定帧率抽帧；
2. YOLOv8n 导出 ONNX，CPU 推理，每帧算 `confidence` / `image_quality` / `infer_time_ms` / `roi_ratio`；
3. 先把 `confidence_calibrated` 填 `null`（第 2–3 周再做温度缩放）；
4. 链路字段先用链路仿真器的占位值（`link_rtt_ms`、`link_loss_rate` 等），动作先固定为 `keep`；
5. 验收：`python scripts/validate_log.py <你的日志> --strict` 通过，且 `read_log.py` 能出 DataFrame。

---

## 协作约定

- 小团队，两个人**都直接 push main**；但改 `schema/` 或 `docs/log_contract.md` 前先在群里说一句，避免同时改同一张表；
- 每次提交前跑上面 3 个命令（后面可以加成 pre-commit hook）；
- 实验产物（视频、`.parquet`、`.onnx`、长日志）**不进 git**，放网盘/Release，见 `.gitignore`；
- 分支只在做大改动时用：`feat/vision-pipeline`、`feat/link-emu` 这类，合并后删除。

---

## 环境

- Python **3.10+**；`scripts/validate_log.py` 与 `scripts/check_contract.py` **零依赖**（标准库即可）；
- `scripts/read_log.py` 装 pandas 后可用 DataFrame 视图：`pip install -r requirements.txt`；
- 仓库内所有文件 UTF-8 无 BOM（Windows 下用编辑器默认 UTF-8 保存即可）。

---

## 为什么叫 PatroLink

**Patro**l（巡检）+ **Link**（链路）：这个项目的全部闭环都发生在"巡检任务"和"链路约束"的交叉处——链路一恶化，巡检系统就得决定传什么、在哪算、什么时候降级。名字即命题。
