# PatroLink

> **巡检（Patrol）+ 链路（Link）闭环**：通信受限条件下的具身视觉推理管线——可观测、可评价、可调度。
> 2026 数智融和大赛 · 老生组 · 2 人团队（本科大二）

---

## 现在到哪一步了

| 步骤 | 内容 | 状态 |
|---|---|---|
| **Step 1** | **共享日志字段表 + 动作契约（v0.1）** | ✅ 完成（本仓库当前内容） |
| Step 2 | 视觉黑箱推理管线跑通，按契约吐日志 | ⬜ 下一步 |
| Step 3 | 置信度校准（温度缩放） | ⬜ |
| Step 4 | 链路仿真与短时预测 | ⬜ |
| Step 5 | 可观测面板（时间线 / 瓶颈归因 / 奖励曲线） | ⬜ |
| Step 6 | 奖励塑形 + 自适应调度 + 对照实验 | ⬜ |

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
├─ docs/
│  ├─ log_contract.md        # ★ 共享时间线日志字段表（v0.1，主交付物）
│  └─ action_contract.md     # 调度动作契约（7 个动作 + reason 标签）
├─ schema/
│  └─ timeline.fields.json   # 机器可读字段契约（校验脚本的唯一数据源）
├─ logs/
│  └─ sample_frames.jsonl    # 手写样例：1 回合 8 个 tick，含降级/未上传/切边
├─ scripts/
│  ├─ validate_log.py        # 按契约校验任意 timeline.jsonl
│  ├─ read_log.py            # 读日志 → 摘要（pandas 可选）
│  └─ check_contract.py      # 文档表格 vs schema 一致性检查
├─ requirements.txt          # 只有 pandas（可选）
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

## 下一步（Step 2：让视觉管线按契约吐日志）

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
