# 链路风险预测器

目标：基于过去 2 s 的链路指标，预测未来 1 s 内链路是否恶化。方法为监督学习二分类，不涉及强化学习与 GPU。

代码：`ml/build_dataset.py`（构建样本）、`ml/train_predictor.py`（训练与评估）、`ml/make_demo_traces.py`（合成演示数据）。

## 1. 标签定义

| 项 | 取值 |
|---|---|
| 特征窗口 W | 2.0 s（过去） |
| 预测窗口 H | 1.0 s（未来） |
| 滑窗步长 S | 0.2 s |
| 恶化判据 | 未来 H 内 RTT P95 ≥ 80 ms，或丢包率 ≥ 3% |

标签只由未来窗口计算，特征只使用过去窗口，两者不重叠。

阈值 80 ms 与 3% 为业务定义（与调度窗口和链路预算相关），在数据采集前确定，不根据结果回调。

## 2. 特征（13 维）

| 类别 | 特征 |
|---|---|
| RTT | `rtt_med`、`rtt_p95`、`rtt_std`、`rtt_min`、`rtt_max`、`rtt_slope`、`rtt_delta` |
| 丢包与抖动 | `loss_rate`、`jitter_med` |
| 信号 | `rssi_med`、`rssi_std`、`rssi_slope` |
| 采样质量 | `valid_ratio` |

不使用位置标签与任何未来统计量。

## 3. 划分与基线

| 划分 | 说明 | 用途 |
|---|---|---|
| `--split trace` | 留一 trace（LOTO），留出条目不参与训练 | 主指标，衡量跨场景泛化 |
| `--split time` | 每条 trace 前 70% 训练、后 30% 测试，中间留 1.2 s 间隔 | 参考指标，衡量同场景外推 |

基线（不训练，必须先比较）：

- `persistence`：当前 RTT P95 已超阈值即判恶化，无提前量；
- `rule`：当前 RTT 中位数 > 0.8×阈值 或丢包率 > 0.5×阈值。

模型：标准化 + 逻辑回归、梯度提升（HistGradientBoosting）。阈值在训练集 5 折 OOF 概率上选取，不在测试集上调参。

## 4. 运行

```powershell
# 数据：真实 trace 采集见 docs/experiment-protocol.md；无数据时先用合成数据验证流程
python ml/make_demo_traces.py
python ml/build_dataset.py --traces "traces/demo/*.csv"

# 训练与评估
python ml/train_predictor.py --data ml/artifacts/dataset.csv --split trace --out ml/artifacts/trace
python ml/train_predictor.py --data ml/artifacts/dataset.csv --split time  --out ml/artifacts/time
```

输出：`metrics.json`（逐 fold 指标与汇总）、`model.joblib`（模型与特征/中位数/阈值/窗口参数）、`roc_pr.png`、`timeseries.png`。

## 5. 指标

| 指标 | 用途 |
|---|---|
| AUC / PR-AUC | 排序能力；类别不平衡时以 PR-AUC 为主 |
| F1 / Recall@FPR≤10% | 可接受误报下的检出能力 |
| 误报次数/分钟（静默 trace） | 调度最关心的代价 |
| 预测提前量 | 恶化发生前首次预警的提前秒数（中位数/P90） |

## 6. 合成数据上的结果

以下为 `ml/make_demo_traces.py` 生成数据的验证结果，**仅用于确认流程可用，不构成实验结论**。

事件 trace（LOTO 平均）：

| 模型 | AUC | F1 | Recall |
|---|---|---|---|
| persistence | 0.945 | 0.702 | 0.552 |
| rule | 0.890 | 0.843 | 0.861 |
| 逻辑回归 | 0.958 | 0.919 | 0.901 |
| 梯度提升 | 0.938 | 0.886 | 0.912 |

全程良好 trace 的误报率：`rule` 1.41–1.62 次/分钟，逻辑回归 0.00，梯度提升 0.00–0.20 次/分钟。

预测提前量中位数 4.8 s（P90 5.0 s，73 个事件；受 5 s 回溯上限约束）。

时间划分在静默 trace 上出现过 12.98 次/分钟误报，原因是训练段缺少负样本。因此测试集必须包含全程良好的 trace，且主指标以 LOTO 为准。

## 7. 与系统的接入

日志契约 v0.2 新增字段：

| 字段 | 内容 |
|---|---|
| `pred_p_degrade` | 预测概率（0–1） |
| `pred_risk` | 风险等级：0 低（p < 0.4）、1 中（0.4 ≤ p < 0.7）、2 高（p ≥ 0.7） |
| `pred_model_hash` | 模型版本标识，便于复现 |

调度器侧要求：升档需连续 2 s 处于低风险后执行（滞回），并记录 `exec_switched`。

## 8. 注意事项

1. 时间序列不得随机打乱划分；窗口重叠会造成信息泄漏。
2. 阈值必须在训练集上选取，不能用测试集调参。
3. 训练与测试的 NaN 填充必须使用训练集中位数。
4. 类别不平衡时不要看 accuracy，以 PR-AUC、F1 与误报率为主。
5. 采集阶段必须包含"全程良好"的 trace，否则误报率无法评估。
6. 预测时域从 1 s 放宽到 2–3 s 时精度会下降，建议以 H=0.5/1/2 s 各训练一次，绘制"时域—精度"曲线。
7. 单次推理耗时低于 1 ms，可直接运行在 100 ms 控制循环内。
