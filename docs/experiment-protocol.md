# 实验与数据采集规范

本文覆盖链路数据采集所需的硬件设置、采集计划、注入方法、产物格式与验收标准。适用于 W1–W2 阶段；后续接入时间线日志后另见 `docs/log-contract.md`。

## 1. 目的与范围

采集目标：获得一批带元数据（距离、遮挡、信道、注入参数）的真实 WiFi 链路 trace，用于训练与评估链路风险预测器，并为后续调度实验提供链路条件。

采集内容：RTT、丢包、RSSI、上下行吞吐。

不在范围内：视觉数据采集、真实 5G/URLLC 专网实验。

## 2. 系统拓扑

```
[PC] 移动热点（2.4 GHz）──WiFi──[ESP32-S3 探针]
 │ 运行 pc/probe_link.py 或 pc/collect_dataset.py
 └ 保存 traces/*.csv|json|png 与 manifest.csv
```

- PC 负责热点、测量与存档；ESP32-S3 负责 UDP 回显、RSSI 上报与吞吐测试服务；
- 链路采集阶段不使用树莓派（其用于后续视觉管线）；PC 热点不可用时可用手机热点，PC 与 ESP32 同时接入；
- ESP32-S3 仅支持 2.4 GHz。Windows 移动热点若仅开放 5 GHz，需在"移动热点 → 编辑"中改为 2.4 GHz。

## 3. 环境准备

### 3.1 热点

1. 设置 → 网络和 Internet → 移动热点；
2. 网络名称 `PatroLink-AP`，密码不少于 8 位；
3. 频段选择 2.4 GHz，打开热点；
4. 记录热点网关地址（`ipconfig`，通常为 192.168.137.1）。

### 3.2 ESP32-S3 固件

1. 编辑 `firmware/esp32s3_probe/esp32s3_probe.ino` 顶部 `WIFI_SSID` 与 `WIFI_PASS`；
2. Arduino IDE 板卡设置：

| 选项 | 值 |
|---|---|
| Board | ESP32S3 Dev Module |
| USB CDC On Boot | Enabled |
| PSRAM | 板载有则选 OPI PSRAM，否则 Disabled |
| Flash Size | 按板卡选择 8MB / 16MB |
| Upload Speed | 921600 |
| Port | 板卡的 UART 口（非 USB 口） |

3. 上传后打开串口监视器（115200），确认输出：

```
[wifi] OK ip=192.168.137.2 rssi=-46 ch=6
[http] listening on 80, udp probe on 9000
[hb] rssi=-46 ch=6 ip=192.168.137.2 heap=2xxxxx up=5s
```

记录板卡 IP，供采集命令的 `--host` 使用。若上传失败，按住 BOOT 后再尝试。

### 3.3 上位机

```powershell
pip install -r requirements.txt
```

首次运行若弹出防火墙提示，允许"专用网络"访问。板卡就绪后可先 `ping <板卡IP>` 确认连通；UDP 探针不依赖 ICMP，ping 不通时仍可尝试采集。

## 4. 采集计划

默认计划见 `plans/collect_plan.json`，共 9 条，约 57 分钟：

| # | label | 时长 | 距离 | 遮挡 | 现场动作 |
|---|---|---|---|---|---|
| 1 | good_desk | 8 min | 1 m | 无 | 设备静止 |
| 2 | good_desk_2 | 8 min | 1 m | 无 | 同场景重复，用于误报评估 |
| 3 | wall_5m | 8 min | 5 m | 一堵墙 | 板卡置于墙另一侧，中间无人走动 |
| 4 | wall_5m_2 | 8 min | 5 m | 一堵墙 | 同场景重复，保持距离一致 |
| 5 | person_3m | 5 min | 3 m | 人 | 一人在两端之间往返走动 |
| 6 | far_corridor | 5 min | 15 m | 墙+门 | 楼道尽头，关闭一扇门 |
| 7 | outage_far | 5 min | 20 m | 墙 | 走出覆盖至断连，停留 3 s 后返回 |
| 8 | inject_delay | 5 min | 5 m | 无 | Clumsy 注入 150 ms 延迟、30 ms 抖动 |
| 9 | inject_loss | 5 min | 5 m | 无 | Clumsy 注入 10% 丢包 |

采集时应主动制造事件（走动、遮挡、开关门、断连），避免 90% 以上样本处于良好状态。

## 5. 采集操作

```powershell
# 查看计划，不执行测量
python pc/collect_dataset.py --host 192.168.137.2 --dry-run

# 按计划采集（每条开始前暂停，等待确认）
python pc/collect_dataset.py --host 192.168.137.2

# 仅采集部分条目
python pc/collect_dataset.py --host 192.168.137.2 --only wall_5m,person_3m
python pc/collect_dataset.py --host 192.168.137.2 --limit 1
```

`Ctrl+C` 可中止，已完成条目已落盘并写入 `manifest.csv`。

### 5.1 可控注入（第 8–9 条）

1. 以管理员身份运行 Clumsy（WinDivert 驱动需要）；
2. 过滤器填写 `udp and udpport 9000`；
3. 第 8 条：启用 `Lag` 150 ms、`Drop` 0%；第 9 条：`Drop` 10%；
4. 点击 `Start` 后开始采集；该条结束后立即 `Stop`，避免影响后续采集；
5. 注入参数已写入计划文件的 `inject` 字段，会记录进 `manifest.csv`。

无法使用 Clumsy 时可跳过第 8–9 条，优先保证前 7 条实测数据。

### 5.2 运行中观察

| 观察点 | 正常表现 | 异常处理 |
|---|---|---|
| ESP32 串口心跳 | 每 2 s 一行，`up` 递增 | `up` 归零表示重启，检查供电 |
| 采集终端（每 5 s 一行） | 丢包 < 2%，RTT 20–30 ms | 丢包 100% 检查 IP/连接；RTT 异常检查信道与干扰 |
| `traces/*.png` | good 平稳，wall/person 出现台阶 | 曲线为直线时检查回包 |

判读原则：恶化通常表现为 RTT 先上升、RSSI 先下降、丢包与重传随后出现。same 场景下 good 与 wall 的 RTT 中位数差异应不小于 10 ms（或 RSSI 差异不小于 5 dBm），否则视为位置设置不当，应重采。

## 6. 产物与元数据

| 文件 | 内容 |
|---|---|
| `traces/probe_<label>_<时间>.csv` | 逐包 `wall_ms, seq, rtt_ms, rssi_dbm, lost` |
| `traces/probe_<label>_<时间>.json` | 汇总统计 + `dataset` 元数据段 |
| `traces/probe_<label>_<时间>.png` | RTT 与 RSSI 曲线 |
| `traces/manifest.csv` | 全部采集条目的记账表 |

`dataset` 元数据字段：`distance_m`、`obstacle`、`channel`、`device`、`hotspot`、`note`、`inject`。采集命令对应参数为 `--distance-m`、`--obstacle`、`--channel`、`--device`、`--hotspot`、`--note`；注入条目在计划文件中声明。

`manifest.csv` 列：`started_at, label, csv, json, duration_s, distance_m, obstacle, channel, hotspot, inject, note, sent, received, loss_rate, rtt_median, rtt_p95, rssi_median`。

## 7. 数据策略与划分

| 层级 | 来源 | 用途 |
|---|---|---|
| L1 实测 | 探针在真实 WiFi 上采集 | 训练与唯一的测试集 |
| L2 可控注入 | 同一链路加 Clumsy 注入 | 扩增事件与消融实验，仅进训练集 |
| L3 纯仿真 | `ml/make_demo_traces.py` | 流程验证与极端场景测试，不作为证据 |

划分要求：

- 主指标采用留一 trace（LOTO）：留出的 trace 完全不参与训练；
- 时间划分仅在单条 trace 内前后切分，训练与测试之间保留至少 1.2 s 间隔，避免滑窗重叠泄漏；
- 测试集必须包含至少一条全程良好的 trace，否则误报率无法评估。

## 8. 验收标准（W1）

- 至少 7 条 trace（含 2 条全程良好），或 9 条完整计划；
- 每条 trace 具备完整元数据，`manifest.csv` 行数与文件数一致；
- 采样率稳定在 20 Hz；除注入条目外丢包率低于 2%；
- 至少一条 trace 包含"断连—重连"过程，且时间戳连续；
- 同一场景的两次采集，RTT 中位数差异不超过 20%。

## 9. 故障排查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 板卡反复输出 `[wifi] FAILED` | 热点为 5 GHz、密码错误或未开启 | 改为 2.4 GHz；核对 SSID/密码；改用手机热点 |
| 采集脚本无法连接 | 板卡 IP 变化（DHCP） | 以串口输出 IP 为准；或在热点中绑定固定 IP |
| 未注入但丢包率超过 20% | 2.4 GHz 拥塞、USB3 干扰、距离过近导致饱和 | 更换信道 1/6/11；板卡远离 USB3 设备；调整距离 |
| 板卡重启（`up` 归零） | USB 供电不足 | 使用带供电的 USB hub 或 5V/1A 电源 |
| 采集中曲线停滞 | 电脑睡眠、热点关闭 | 电源计划设为高性能；关闭睡眠；重采该条 |
| Clumsy 无效果 | 过滤器错误或未以管理员运行 | 过滤器 `udp and udpport 9000`；以管理员运行 |
| Python 被防火墙拦截 | 首次运行未放行 | 允许专用网络访问 |

## 10. 采集记录

每条采集自动写入 `manifest.csv`。建议在 `docs/experiment-log.md`（或组会记录）中补充人工备注：断连发生的大致时刻、人员走动区间、环境变化（开关门、搬动设备），便于后续解释异常区段。
