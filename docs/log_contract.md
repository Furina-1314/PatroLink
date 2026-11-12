# 共享时间线日志字段表（log contract v0.1）

> **一句话**：所有模块（视觉、链路、调度、奖励、面板）只往 **一条** `timeline.jsonl` 里写数，面板和指标只从这一条里读——先把"账本格式"定死，后面写代码才不会各说各话。
> 状态：v0.1 草案（2026-11-12）｜维护人：两个人一起改｜机器可读版本：`schema/timeline.fields.json`（本页表格由它生成，改字段请同步改两处并跑 `python scripts/check_contract.py`）

---

## 一、三条硬规则（先记这三条，其余都是细节）

1. **时间基准统一用 Unix 毫秒时间戳（int, UTC）**。帧采集时刻写 `t_frame_ms`，本次决策完成时刻写 `t_ms`；端到端时延 = `t_ms − t_frame_ms`，没有第二个口径。
2. **一行 = 一个决策时刻（`event":"tick"`）**。不是一帧一行，也不是一个字段一行；一个 tick 里把这一时刻的视觉、链路、动作、执行、奖励全写齐。
3. **字段名和单位定死，新增字段走版本号**。任何新增/改名都在 `schema/timeline.fields.json` 里加一条、升版本、同步本页和样例日志，然后两边重新拉一次。**不要在代码里悄悄加字段。**

---

## 二、为什么需要这份契约

- 视觉侧算出来的 `confidence`、链路侧算出来的 `link_rtt_ms`、调度器选出的 `act_name`，**必须落在同一个时间戳上**，面板才能把"这一帧为什么慢"讲清楚；
- 没有契约，就会出现"我这边叫 `latency`、你那边叫 `delay_ms`""我用秒、你用毫秒"，最后对不上账；
- 契约也是**验收工具**：校验脚本能自动发现缺字段、类型错、时延对不上，比两个人互相看代码便宜得多。

---

## 三、时间戳与对齐规则

```
t_frame_ms ──采集──▶ 预处理 ──▶ 上传 ──▶ 推理 ──▶ 决策 ──▶ t_ms
                                                          │
                    link_rtt_ms / pred_p_degrade_1s 取自同一窗口 ┘
```

- 所有模块用**同一个时钟**（`time.time_ns() // 1_000_000`），不要用 `datetime.now()` 的本地时间字符串；
- `t_frame_ms` 是**采集**时刻，`t_ms` 是**决策完成**时刻，两者之差就是 `exec_t_total_ms`（校验脚本会比对，容差 1.5 ms）；
- 链路事件与帧事件靠时间戳窗口对齐：链路采样比决策密（如每 50 ms 一次），写日志时取"本决策窗口内最后一次采样"或窗口统计量，**不要跨窗口平均**；
- 常见错误：把日志写盘时刻当成 `t_ms`。写盘时刻只在文件里做调试参考，**不进入契约**。

---

## 四、字段表

### 4.1 通用字段

| 字段 | 含义 | 单位 / 取值 | 必填 | 产生方 | 使用方 |
|---|---|---|---|---|---|
| `schema_version` | 契约版本；不兼容改动时升 0.2。校验脚本据此拒绝跨版本混写。 | 固定 `0.1` | ✅ tick | 共同 | 校验脚本 |
| `event` | 事件类型。tick = 一次调度决策时刻，是日志的主体。 | `episode_start` / `tick` / `episode_end` | ✅ tick | 共同 | 面板 / 指标 |
| `run_id` | 一次回合的唯一标识，约定命名 r{路线序号}_{场景}_{策略}_{种子}，如 r01_routeA_good_s1。 | 字符串 | ✅ tick | 实验脚本 | 所有人 |
| `ep_id` | 回合序号，从 0 或 1 开始均可，同一 run 内单调递增。 | 整数，≥ 0 | ✅ tick | 实验脚本 | 所有人 |
| `t_ms` | 本行日志的产生时刻（决策完成时刻）。不是帧采集时刻，也不是文件写入时刻。 | Unix 毫秒，≥ 0 | ✅ tick | 日志写入方 | 时间线对齐 |
| `policy` | 只写在 episode_start 上。B0 固定基线 / B1 仅当前状态 / B2 预测+固定权重 / Ours 完整方案 / Oracle 已知未来（上界）。 | `B0` / `B1` / `B2` / `Ours` / `Oracle` | 建议 | 实验脚本 | 指标 / 对照 |

### 4.2 帧侧字段

| 字段 | 含义 | 单位 / 取值 | 必填 | 产生方 | 使用方 |
|---|---|---|---|---|---|
| `route` | 采集路线：A 走廊 / B 实验室 / C 楼梯口。 | `A` / `B` / `C` | 建议 | 具身侧 | 指标 / 面板 |
| `frame_id` | 帧序号，同一 run 内单调递增；是帧表、缓存表、日志三者的关联主键。 | 整数，≥ 0 | ✅ tick | 具身侧 | 所有人 |
| `t_frame_ms` | 该帧的采集时刻。端到端时延 = t_ms − t_frame_ms。 | Unix 毫秒，≥ 0 | ✅ tick | 具身侧 | 时延 / 归因 |
| `view` | 视角：far 远视角 / near 接近目标重拍（具身动作的结果）。 | `far` / `near` | 建议 | 具身侧 | 调度 / 面板 |
| `confidence` | 模型原始检测置信度（未校准）。 | 0–1，0.0–1.0 | ✅ tick | 具身侧 | 调度 / 奖励 |
| `confidence_calibrated` | 温度缩放后的置信度；未做校准前填 null（第 2–3 周上线）。调度阈值只用这个字段。 | 0–1，0.0–1.0 | 建议，可 null | 具身侧 | 调度 / 奖励 |
| `image_quality` | 轻量图像质量分：拉普拉斯方差（清晰度）与亮度归一化后的加权值，0 最差 1 最好。 | 0–1，0.0–1.0 | ✅ tick | 具身侧 | 调度 / 奖励 |
| `infer_time_ms` | 本次推理耗时（perf_counter 计时），端侧与边缘侧各自记录。 | 毫秒，≥ 0.0 | ✅ tick | 具身侧 | 瓶颈归因 |
| `roi_ratio` | 目标 ROI 面积占全帧比例；为 0 表示本帧没有目标。 | 0–1，0.0–1.0 | ✅ tick | 具身侧 | 裁剪决策 |
| `mode` | 本帧实际推理位置：device 端侧（量化小模型）/ edge 边缘（完整模型）。 | `device` / `edge` | ✅ tick | 具身侧 | 调度 / 归因 |
| `resolution` | 本帧实际使用的分辨率档。动作生效后，下一帧必须反映新档位。 | `1920x1080` / `1280x720` / `640x480` / `320x240` | ✅ tick | 具身侧 | 降级动作 / 面板 |
| `suggest_action` | 管线自己的建议（不含调度权重），供调度器参考；null 表示没有建议。 | 枚举或 null：`keep` / `downscale` / `lower_fps` / `crop_roi` / `switch_edge` / `switch_device` / `skip_upload` | 可选，可 null | 具身侧 | 调度器参考 |

### 4.3 链路侧字段

| 字段 | 含义 | 单位 / 取值 | 必填 | 产生方 | 使用方 |
|---|---|---|---|---|---|
| `link_rtt_ms` | 本决策窗内的上行 RTT 估计（最近一个采样窗口的中位数）。 | 毫秒，≥ 0.0 | ✅ tick | 链路侧 | 调度 / 指标 |
| `link_jitter_ms` | RTT 抖动（同一窗口内的标准差或 P90−P10）。 | 毫秒，≥ 0.0 | 建议 | 链路侧 | 调度 / 面板 |
| `link_loss_rate` | 丢包率，小数不是百分数（0.02 = 2%）。 | 0–1，0.0–1.0 | ✅ tick | 链路侧 | 调度 / 指标 |
| `link_thr_mbps` | 上行可用吞吐估计。 | Mbps，≥ 0.0 | 建议 | 链路侧 | 调度 / 指标 |
| `link_zone` | 链路场景标记：良好 / 遮挡 / 人多 / 干扰 / 切换 / 中断。用于把时间线按场景切段。 | `good` / `occlusion` / `crowd` / `interference` / `handover` / `outage` | 建议 | 链路侧 | 面板 / 归因 |
| `pred_p_degrade_1s` | 预测未来 1 s 内链路恶化（RTT 或丢包超阈值）的概率；预测器未上线时填 null。 | 0–1，0.0–1.0 | 建议，可 null | 链路侧 | 调度器（前馈） |
| `pred_rtt_p95_ms` | 预测窗口内 RTT 的 P95；用于估算本次上传是否会超时。 | 毫秒，≥ 0.0 | 可选，可 null | 链路侧 | 调度器（前馈） |

### 4.4 调度侧字段

| 字段 | 含义 | 单位 / 取值 | 必填 | 产生方 | 使用方 |
|---|---|---|---|---|---|
| `act_name` | 本决策时刻选中的动作，取值见 docs/action_contract.md。 | `keep` / `downscale` / `lower_fps` / `crop_roi` / `switch_edge` / `switch_device` / `skip_upload` | ✅ tick | 调度器 | 执行器 / 面板 |
| `act_params` | 动作参数，如 {"resolution":"320x240"}；无参数时写 {}，不要省略。 | 对象 | ✅ tick | 调度器 | 执行器 |
| `act_reason` | 动作依据的机器可读标签（禁止中文长句），如 pred_degrade_0.62、conf_below_tau。 | ASCII 短串 | ✅ tick | 调度器 | 面板 / 答辩 |

### 4.5 执行侧字段

| 字段 | 含义 | 单位 / 取值 | 必填 | 产生方 | 使用方 |
|---|---|---|---|---|---|
| `exec_t_total_ms` | 端到端时延，必须等于 t_ms − t_frame_ms（校验脚本会比对，容差 1 ms）。 | 毫秒，≥ 0.0 | ✅ tick | 执行器 | 指标 / 归因 |
| `exec_t_uplink_ms` | 其中上传段耗时；不上传时为 0。 | 毫秒，≥ 0.0 | 建议 | 执行器 | 瓶颈归因 |
| `exec_bytes_up` | 本帧实际上行字节数；缓存不上传时为 0。 | 字节，≥ 0 | ✅ tick | 执行器 | 流量指标 / 奖励 |
| `exec_retx` | 本帧重传次数。 | 次，≥ 0 | 建议 | 执行器 | 归因（重传瓶颈） |
| `exec_success` | 本时刻是否推进了任务（目标被正确上报或状态被有效更新）。 | 0/1：`0` / `1` | ✅ tick | 执行器 | 任务成功率 |
| `exec_switched` | 本时刻是否发生了动作切换（用于计算切换频次与控制策略抖动）。 | 0/1：`0` / `1` | 建议 | 执行器 | 切换频次指标 |

### 4.6 奖励侧字段

| 字段 | 含义 | 单位 / 取值 | 必填 | 产生方 | 使用方 |
|---|---|---|---|---|---|
| `rw_total` | 本时刻奖励总分 = 各分量之和（口径见 docs/log_contract.md §4.9）。 | 无量纲 | ✅ tick | 奖励模块 | 指标 / 对照实验 |
| `rw_task` | 任务推进分量。 | 无量纲 | 建议 | 奖励模块 | 面板 / 一致性分析 |
| `rw_latency` | 时延惩罚分量（通常为负）。 | 无量纲 | 建议 | 奖励模块 | 面板 / 一致性分析 |
| `rw_bytes` | 上行流量惩罚分量（通常为负）。 | 无量纲 | 建议 | 奖励模块 | 面板 / 一致性分析 |
| `rw_conf_margin` | 置信度裕度分量（校准置信度高于阈值的部分）。 | 无量纲 | 建议 | 奖励模块 | 面板 |
| `rw_switch` | 切换代价分量（通常为负）。 | 无量纲 | 建议 | 奖励模块 | 面板 |
| `rw_lambda_lat` | 时延约束的拉格朗日乘子（对偶上升后的当前值），用于解释"为什么此刻偏向低时延"。 | 乘子，≥ 0.0 | 建议 | 奖励模块 | 答辩证据 |
| `why` | 一句话解释本次决策，面板直接显示；用于验收"面板不是装饰"。 | 中文短句 | 建议 | 调度器 | 面板 / 答辩 |

### 4.7 回合汇总字段（只写在 episode_end）

| 字段 | 含义 | 单位 / 取值 | 必填 | 产生方 | 使用方 |
|---|---|---|---|---|---|
| `ep_success` | 只写在 episode_end：本回合全部目标是否在时限内完成。 | 0/1：`0` / `1` | 建议 | 实验脚本 | 任务成功率 |
| `ep_reward_sum` | 只写在 episode_end：回合奖励总和。 | 无量纲 | 建议 | 实验脚本 | 奖励-性能一致性 |
| `ep_bytes_up` | 只写在 episode_end：本回合总上行字节。 | 字节，≥ 0 | 建议 | 实验脚本 | 流量指标 |
| `ep_p95_lat_ms` | 只写在 episode_end：本回合端到端时延 P95。 | 毫秒，≥ 0.0 | 建议 | 实验脚本 | 时延指标 |
| `ep_switches` | 只写在 episode_end：本回合动作切换总次数。 | 次，≥ 0 | 建议 | 实验脚本 | 切换频次指标 |

### 4.8 奖励口径（`rw_total` 怎么来）

```
rw_total = rw_task + rw_latency + rw_bytes + rw_conf_margin + rw_switch
```

- `rw_task`：任务推进（成功上报 +，未推进 0）；
- `rw_latency = −λ_lat · max(0, exec_t_total_ms − L_budget) / L_budget`，`L_budget` 取 100 ms；
- `rw_bytes = −λ_bw · exec_bytes_up / B_avg`，`B_avg` 取一张 640x480 帧的字节数；
- `rw_conf_margin = η · max(0, confidence_calibrated − τ)`，`τ` 取 0.6；
- `rw_switch = −w_sw · 1[exec_switched=1]`；
- `λ_lat` 用对偶上升在线更新：`λ_lat ← max(0, λ_lat + ρ·(实测超限率 − ε))`，`ρ=0.05`、`ε=0.05`；初值 0.2。
- 权重与乘子都要写进同一行日志（`rw_lambda_lat`），否则"为什么这一刻偏向低时延"无法解释；
- 样例日志里的 `rw_lambda_lat` 为演示值（`0.20 + 0.9·p_degrade`），正式实验按上面的对偶上升式在线更新。

### 4.9 枚举字典

| 字段 | 合法取值 | 说明 |
|---|---|---|
| `event` | `episode_start` / `tick` / `episode_end` | 回合开始 / 决策时刻 / 回合结束 |
| `policy` | `B0` / `B1` / `B2` / `Ours` / `Oracle` | 固定基线 / 仅当前状态 / 预测+固定权重 / 完整方案 / 已知未来上界 |
| `mode` | `device` / `edge` | 端侧推理（量化小模型）/ 边缘推理（完整模型） |
| `resolution` | `1920x1080` / `1280x720` / `640x480` / `320x240` | 分辨率档位 |
| `view` | `far` / `near` | 远视角 / 接近目标重拍 |
| `link_zone` | `good` / `occlusion` / `crowd` / `interference` / `handover` / `outage` | 链路场景标记 |
| `act_name` | 见 `docs/action_contract.md`（7 个） | 调度动作 |
| `suggest_action` | 同 `act_name`，另可 `null` | 管线建议，不参与评分 |

> **命名约定**：蛇形小写；帧侧不加前缀，链路侧 `link_`/`pred_`，调度侧 `act_`，执行侧 `exec_`，奖励侧 `rw_`，回合汇总 `ep_`。单位一律写在表里，不在字段名里重复（写 `_ms` 只表示毫秒，不再写 `_ms_ms`）。

---

## 五、一行完整样例（真实存在于 `logs/sample_frames.jsonl`）

```json
{"schema_version":"0.1","event":"tick","run_id":"r01_routeA_good_s1","ep_id":1,"frame_id":3,"t_frame_ms":1794441600300,"t_ms":1794441600418,"route":"A","view":"far","confidence":0.61,"confidence_calibrated":0.57,"image_quality":0.52,"infer_time_ms":36.1,"roi_ratio":0.21,"mode":"device","resolution":"640x480","suggest_action":"downscale","link_rtt_ms":96.3,"link_jitter_ms":18.4,"link_loss_rate":0.021,"link_thr_mbps":12.4,"link_zone":"occlusion","pred_p_degrade_1s":0.62,"pred_rtt_p95_ms":148.0,"act_name":"downscale","act_params":{"resolution":"320x240"},"act_reason":"pred_degrade_0.62","exec_t_total_ms":118.4,"exec_t_uplink_ms":96.0,"exec_bytes_up":18432,"exec_retx":0,"exec_success":1,"exec_switched":1,"rw_total":0.0737,"rw_task":0.55,"rw_latency":-0.1398,"rw_bytes":-0.2765,"rw_conf_margin":0.0,"rw_switch":-0.06,"rw_lambda_lat":0.76,"why":"预测 1 s 内链路恶化 → 提前降档 320x240"}
```

**读法**：第 3 帧在 `t_frame_ms` 采集 → 预测到 1 s 内链路会恶化（`pred_p_degrade_1s=0.62`）→ 调度器提前降档到 320x240（`act_name=downscale`）→ 端到端 118.4 ms（其中上传 96 ms）→ 奖励被时延和流量分扣掉，`rw_total` 已经贴近 0（再差一点就该缓存不传了）。

---

## 六、校验：每次提交前跑一遍

```bash
# 基础校验（缺字段 / 类型错 / 枚举越界 / 时延对不上）
python scripts/validate_log.py logs/sample_frames.jsonl

# 带统计摘要（行数、成功率、P50/P95 时延、动作分布）
python scripts/validate_log.py logs/sample_frames.jsonl --summary

# 批量校验，并把警告也当失败（CI 用）
python scripts/validate_log.py "logs/*.jsonl" --strict
```

校验脚本会检查：必填字段、字段类型、枚举取值、数值范围、`schema_version` 一致、`exec_t_total_ms ≈ t_ms − t_frame_ms`、`rw_total ≈ 分量之和`、`tick` 是否出现在 `episode_start` 之前、是否有重复字段。

**警告级别（`--strict` 才失败）**：未登记字段、时延/奖励对不上、`tick` 早于 `episode_start`。

---

## 七、版本与变更流程

1. 想加/改字段 → 在 `schema/timeline.fields.json` 里改，并在 `changelog` 追加一条；
2. 跑 `python scripts/check_contract.py`（检查本页表格与 schema 是否一致），再跑 `python scripts/validate_log.py logs/sample_frames.jsonl --strict`；
3. 两人都确认后，版本号 +0.1（不兼容改名 → 主版本 +1），样例日志同步更新；
4. PR 描述里写一句"为什么改"（例：`link_zone` 需要区分 handover，否则归因把切换成当成遮挡）。

**当前版本**：`0.1`（2026-11-12）首版。

---

## 八、第 1 周结束前还要定的 4 件事

- [ ] 日志粒度：确认"一决策一行"够用（若需要每帧都记，加 `event:"frame"`，成本是日志体积 ×N）
- [ ] 链路采样频率：50 ms / 100 ms，以及窗口统计用中位数还是均值
- [ ] 回合长度：单回合固定 60 s 还是按目标数结束（影响 `ep_success` 定义）
- [ ] 文件切分：一个 run 一个文件（`logs/{run_id}.jsonl`）还是 一个策略一个文件（便于对比）

---

## 九、面板与指标怎么读（pandas 一行）

```python
import pandas as pd
df = pd.read_json("logs/sample_frames.jsonl", lines=True)
ticks = df[df.event == "tick"]          # 只取决策时刻
print(ticks[["frame_id", "exec_t_total_ms", "act_name", "rw_total"]].head())
print(ticks.exec_t_total_ms.quantile([0.5, 0.95]))   # 时延 P50 / P95
```

面板的三条验收线（方案 §六）：能回答"这一帧慢在哪一段""换个档位哪段变了""奖励里谁在主导"——都只依赖这份日志，不手工补数。
