# 调度动作契约（action contract v0.1）

> 动作是**调度器 → 执行器**的唯一接口。调度器只输出"做什么"，执行器负责"怎么做"，两者通过下面这个 JSON 结构对齐；动作执行结果回写到同一条 `timeline.jsonl`（字段见 `docs/log_contract.md`）。

---

## 一、动作 JSON 结构

```json
{
  "run_id": "r01_routeA_good_s1",
  "ep_id": 1,
  "t_ms": 1794441600418,
  "act_name": "downscale",
  "act_params": {"resolution": "320x240"},
  "act_reason": "pred_degrade_0.62"
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `run_id` / `ep_id` | ✅ | 与日志同一套标识 |
| `t_ms` | ✅ | 决策时刻（Unix 毫秒），与日志 tick 的 `t_ms` 完全一致 |
| `act_name` | ✅ | 7 个动作之一，见下表 |
| `act_params` | ✅ | 动作参数；无参数写 `{}`，**不允许省略** |
| `act_reason` | ✅ | 机器可读标签（ASCII，≤64 字符），见 §三 |

**时序规则**：tick `t` 做出决策 → 作用于**下一帧**（`frame_id+1`）→ 下一帧日志必须反映动作效果。同一 tick 只允许**一个**动作；v0.1 不支持多动作串联（需要时用参数表达，例如 `downscale` 的 `resolution`）。

---

## 二、7 个动作

| # | `act_name` | 语义 | `act_params` | 执行器要做什么 | 下一帧日志必须反映 |
|---|---|---|---|---|---|
| 1 | `keep` | 保持当前配置 | `{}` | 什么都不改 | `resolution` / `mode` / `view` 与上一帧相同 |
| 2 | `downscale` | 降分辨率档 | `{"resolution":"1280x720"\|"640x480"\|"320x240"}` | 预处理阶段缩放输入，再推理 | `resolution` 等于新档位 |
| 3 | `lower_fps` | 降抽帧帧率 | `{"fps":10\|5\|2}` | 调整抽帧间隔（丢弃部分帧） | 相邻 tick 的 `t_frame_ms` 间隔变大、`frame_id` 跳号 |
| 4 | `crop_roi` | 只处理/上传 ROI | `{"roi":"T03"}` 或 `{"bbox":[x,y,w,h]}` | 按 ROI 裁剪后再推理/上传 | `roi_ratio` 反映裁剪后占比，`exec_bytes_up` 明显下降 |
| 5 | `switch_edge` | 切到边缘推理 | `{}` | 把整帧（或 ROI）上传到边缘进程，用完整模型推理 | `mode="edge"`，`exec_t_uplink_ms > 0`，`exec_bytes_up > 0` |
| 6 | `switch_device` | 切回端侧推理 | `{}` | 停止上传，用端侧量化模型推理 | `mode="device"`，`exec_t_uplink_ms = 0`，`exec_bytes_up = 0` |
| 7 | `skip_upload` | 本帧缓存、不上传 | `{}` | 帧写入本地缓存队列，跳过上传 | `exec_bytes_up = 0`；`exec_success` 可能为 0（任务未推进要如实记） |

**约束**：
- `sync`/模式切换（5、6）之后**至少保持 1 帧**，避免来回抖动；`exec_switched` 只在真正切换的那一帧记 1；
- `crop_roi` 仅在 `roi_ratio > 0` 时合法；无目标帧调用视为无效动作（校验时按警告处理）；
- `downscale` 不允许放大（只允许往更低的档位走，恢复靠 `keep` 配合链路恢复后再升档）。

---

## 三、`act_reason` 标签表（机器可读，禁止中文长句）

| 标签 | 含义 | 典型触发条件 |
|---|---|---|
| `link_good_conf_ok` | 链路好、置信度高 | `link_rtt_ms < 50` 且 `confidence_calibrated ≥ τ` |
| `conf_ok_bandwidth_ok` | 置信度够、带宽够 | 同上但带宽更充裕 |
| `pred_degrade_<p>` | 预测即将恶化 | `pred_p_degrade_1s ≥ 0.5`，如 `pred_degrade_0.62` |
| `loss_<r>_hold` | 丢包超限，缓存等待 | `link_loss_rate ≥ 0.03`，如 `loss_0.052_hold` |
| `conf_below_tau_link_ok` | 置信度不足、链路允许上传 | `confidence_calibrated < τ` 且 `link_rtt_ms < 100` |
| `bandwidth_limited` | 带宽不足，只能传 ROI | `link_thr_mbps < 20` 且 `roi_ratio > 0` |
| `link_recovered` | 链路恢复，升档/切回端侧 | `link_zone=good` 且 `pred_p_degrade_1s < 0.2` |
| `edge_only_roi` | 只能边缘推理且只传 ROI | `mode=edge` 且 `link_thr_mbps` 低 |

> 新增标签：先在这里登记一行，再在代码里用；`act_reason` 要能被面板直接翻译成一句人话。

---

## 四、7 条可直接粘贴的样例

```json
{"run_id":"r01_routeA_good_s1","ep_id":1,"t_ms":1794441600108,"act_name":"keep","act_params":{},"act_reason":"link_good_conf_ok"}
{"run_id":"r01_routeA_good_s1","ep_id":1,"t_ms":1794441600418,"act_name":"downscale","act_params":{"resolution":"320x240"},"act_reason":"pred_degrade_0.62"}
{"run_id":"r01_routeA_good_s1","ep_id":1,"t_ms":1794441600522,"act_name":"lower_fps","act_params":{"fps":5},"act_reason":"pred_degrade_0.71"}
{"run_id":"r01_routeA_good_s1","ep_id":1,"t_ms":1794441600626,"act_name":"crop_roi","act_params":{"roi":"T03"},"act_reason":"bandwidth_limited"}
{"run_id":"r01_routeA_good_s1","ep_id":1,"t_ms":1794441600714,"act_name":"switch_edge","act_params":{},"act_reason":"conf_below_tau_link_ok"}
{"run_id":"r01_routeA_good_s1","ep_id":1,"t_ms":1794441600802,"act_name":"switch_device","act_params":{},"act_reason":"link_recovered"}
{"run_id":"r01_routeA_good_s1","ep_id":1,"t_ms":1794441600876,"act_name":"skip_upload","act_params":{},"act_reason":"loss_0.052_hold"}
```

---

## 五、执行器的回写义务（写完动作必须回写这 6 个字段）

`act_name` / `act_params` / `act_reason`（调度器写） + `exec_t_total_ms` / `exec_bytes_up` / `exec_success`（执行器写）。

任何动作执行完，都必须能在 `timeline.jsonl` 里找到"动作 + 代价 + 是否推进任务"三件套；否则这个动作不算被执行过。
