# 明天采集操作手册（链路 trace）

> 一句话分工：**电脑开热点 + 跑采集；ESP32-S3 烧固件回包；树莓派不参与**（它是后面视觉管线才用的）。
> 预计：设置 15 分钟 + 采集 57 分钟（默认 9 条）；最低可行 5 条约 35 分钟。
> 全程只需要一条命令，数据自动落盘 + 自动记账。

---

## 〇、谁做哪一步

| 角色 | 做什么 | 明天要不要动 |
|---|---|---|
| **电脑（Windows）** | 开 2.4 GHz 热点、跑采集脚本、看实时进度、存 CSV/JSON/PNG | ✅ 要 |
| **ESP32-S3** | 烧探针固件、连热点、回 UDP/HTTP、串口上报 RSSI | ✅ 要 |
| **树莓派 5** | 视觉管线（摄像头/端侧推理）才用 | ❌ 不用 |
| **手机** | 备用热点（电脑热点只支持 5 GHz 时用） | 🟡 备用 |
| **Clumsy**（电脑软件） | 最后两条"可控注入"用 | 🟡 只在第 8–9 条用 |

---

## 一、采集前 15 分钟：设置清单

### 1.1 电脑：开 2.4 GHz 移动热点（3 分钟）

1. `设置 → 网络和 Internet → 移动热点`
2. 点"编辑"：**网络名称** `PatroLink-AP`、**密码** 至少 8 位（如 `patrolink2026`）
3. **频段选 2.4 GHz**（Win11 在"编辑"里有这个选项；Win10 默认就是 2.4 GHz）
4. 打开"移动热点"
5. 记下电脑在热点里的网关 IP：`ipconfig` → 找"本地连接* 10"或"移动热点"那块的 **默认网关**（通常是 `192.168.137.1`）

> ⚠️ ESP32-S3 只支持 2.4 GHz。如果热点只有 5 GHz 且改不了 → 改用手机热点（电脑和板子都连手机，电脑跑采集不受影响，只是 IP 网段变成 `192.168.x.x`）。

### 1.2 ESP32-S3：烧探针固件（8 分钟）

1. 打开 [`firmware/esp32s3_probe/esp32s3_probe.ino`](../firmware/esp32s3_probe/esp32s3_probe.ino)，**只改两行**：
   ```cpp
   #define WIFI_SSID "PatroLink-AP"
   #define WIFI_PASS "patrolink2026"
   ```
2. Arduino IDE 选板子（工具菜单）：

   | 选项 | 值 |
   |---|---|
   | Board | **ESP32S3 Dev Module** |
   | USB CDC On Boot | **Enabled** |
   | PSRAM | 板子有就选 OPI PSRAM，没有就 Disabled |
   | Flash Size | 8MB / 16MB（按板子） |
   | Upload Speed | 921600 |
   | Port | 板子的 COM 口（插 **UART 口**，不是 USB 口） |

3. 点上传（第一次不行就**按住 BOOT 再点上传**，看到 "Writing" 后松开）
4. 打开串口监视器，波特率 **115200**，应看到：
   ```
   === PatroLink ESP32-S3 link probe v0.1 ===
   [wifi] OK ip=192.168.137.2 rssi=-46 ch=6
   [http] listening on 80, udp probe on 9000
   [hb] rssi=-46 ch=6 ip=192.168.137.2 heap=2xxxxx up=5s
   ```
5. **把这个 IP 抄下来**（如 `192.168.137.2`），下一步要用

### 1.3 电脑：装依赖 + 自检（4 分钟）

```powershell
cd D:\projects\PatroLink
pip install -r requirements.txt

# 板子还没连好时，可先用假节点验证电脑侧（可选）
python tools\fake_esp32_probe.py                  # 终端 A，Ctrl+C 退出
python pc\probe_link.py --host 127.0.0.1 --udp-port 9000 --http-port 8080 --duration 5 --label selftest
```

- 首次运行会弹 **Windows 防火墙**：选"允许访问"（专用网络）。
- 板子接上后先 `ping 192.168.137.2` 确认能通（不通也能采，UDP 探针不依赖 ping）。

---

## 二、开始采集：一条命令

```powershell
# 先空跑看计划（不测量）
python pc\collect_dataset.py --host 192.168.137.2 --dry-run

# 正式采集（每条开始前会暂停，等你按 Enter）
python pc\collect_dataset.py --host 192.168.137.2
```

- 默认计划：[`plans/collect_plan.json`](../plans/collect_plan.json)，9 条、约 57 分钟；
- 每条开始前，脚本会打印"距离/遮挡/备注"，**你摆好位置再按 Enter**；
- `Ctrl+C` 可随时中止，**已完成的部分不会丢**（CSV/JSON 已落盘、`manifest.csv` 已记账）；
- 只想先试一条：`--limit 1`；只想采某几条：`--only wall_5m,person_3m`。

产物（`traces/`）：

| 文件 | 内容 |
|---|---|
| `probe_<label>_<时间>.csv` | 逐包 `wall_ms, seq, rtt_ms, rssi_dbm, lost` |
| `probe_<label>_<时间>.json` | 汇总 + 元数据（距离/遮挡/信道/热点/备注） |
| `probe_<label>_<时间>.png` | RTT 与 RSSI 曲线 |
| `manifest.csv` | 所有采集的记账表（一行一条） |

---

## 三、采什么：9 条计划 + 现场怎么摆

| # | label | 时长 | 距离 | 遮挡 | 现场动作 |
|---|---|---|---|---|---|
| 1 | `good_desk` | 8 min | 1 m | 无 | 板子放桌上不动，电脑也放着别动 |
| 2 | `good_desk_2` | 8 min | 1 m | 无 | 同位置再来一遍（用于误报评估） |
| 3 | `wall_5m` | 8 min | 5 m | 一堵墙 | 板子放墙另一边，中间别站人 |
| 4 | `wall_5m_2` | 8 min | 5 m | 一堵墙 | 同位置第二遍，注意距离一致 |
| 5 | `person_3m` | 5 min | 3 m | 人 | 请同学在板子与电脑之间**来回走**，第 2 分钟起 |
| 6 | `far_corridor` | 5 min | 15 m | 墙+门 | 楼道尽头，关一扇门 |
| 7 | `outage_far` | 5 min | 20 m | 墙 | **走出去直到断连，停 3 s 再走回来**，记下断连时刻 |
| 8 | `inject_delay` | 5 min | 5 m | 无 | 先用 Clumsy 注入（见下），再按 Enter |
| 9 | `inject_loss` | 5 min | 5 m | 无 | 把 Clumsy 改成 10% 丢包，再按 Enter |

**核心纪律**：不要只采"好链路"。第 5、6、7 条才是模型能学到"变差前兆"的关键；采的时候**主动制造**遮挡、走动、断连。

### 3.1 Clumsy 怎么设（第 8–9 条）

1. 下载 Clumsy（免费，单文件）：搜索 `Clumsy download`，解压即用；
2. 以管理员运行（WinDivert 驱动需要）；
3. 过滤器填：`udp and udpport 9000`
4. 勾选：
   - 第 8 条：`Lag` 150 ms + `Drop` 0% + `Out of Order` 0（抖动用 Lag 的 variation 30 ms，若界面支持）
   - 第 9 条：`Drop` 10%
5. 点 `Start`；**采完立刻点 `Stop`**（否则会影响后面所有网络）
6. 把参数写进计划里的 `note`（已写好），manifest 里会带上

> 没有 Clumsy 也能做第 8–9 条：把计划里的两条去掉，或只跑 `--only` 前 7 条。**L1 实测数据比注入更重要。**

---

## 四、采集过程中怎么观察（三个窗口同时开）

| 窗口 | 看什么 | 正常长什么样 | 异常怎么办 |
|---|---|---|---|
| **① ESP32 串口监视器** | 心跳行 `[hb] rssi=... up=...` | 每 2 s 一行，`up` 秒数递增 | `up` 归零 = 板子重启（供电不足）；没有输出 = 串口/波特率错 |
| **② 采集终端（实时行）** | 每 5 s：`收 298/300 丢 0.7% ｜ RTT 23.1 ms ｜ RSSI -46 dBm` | 丢包 <2%，RTT 20–30 ms 稳定 | 丢包 100% = IP 错/板子掉线；RTT >100 ms 且没遮挡 = 信道拥挤 |
| **③ `traces/` 目录** | 每条结束生成 `.png` | 曲线平稳（good）或台阶抬升（wall/person） | 曲线是一条直线 = 探针没收到回包 |

**判读口诀**：
- 移动/遮挡时 **RTT 先抬、RSSI 先掉、丢包最后才来** → 这正是模型要学的前兆；
- `good` 与 `wall` 两条的 RTT 中位数**至少差 10 ms**（或 RSSI 差 5 dBm），否则说明位置没摆对，重采；
- 出现"断连→重连"的那条（`outage_far`）最有价值，别嫌它慢。

---

## 五、采完立刻做 3 件事

```powershell
# 1) 看记账表：应该有 9 行
Get-Content traces\manifest.csv

# 2) 抽两条曲线看差异（good vs wall）
Start-Process traces\probe_good_desk_*.png
Start-Process traces\probe_wall_5m_*.png

# 3) 直接训第一版模型（1 分钟）
python ml\build_dataset.py --traces "traces/probe_*.csv"
python ml\train_predictor.py --data ml\artifacts\dataset.csv --split trace --out ml\artifacts\real
```

打开 `ml\artifacts\real\timeseries.png`：**预测概率应该在橙色"真实恶化"区间之前抬头**。如果完全没有提前量，先别调模型，检查数据（是不是事件太少/位置没变）。

---

## 六、故障速查（现场版）

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 板子串口反复 `[wifi] FAILED` | 热点是 5 GHz / 密码错 / 热点没开 | 改 2.4 GHz；核对 SSID 密码；手机热点替代 |
| 采集脚本报连不上 | IP 变了（DHCP） | 看串口新 IP；或在热点里给板子绑定固定 IP |
| 收发正常但丢包 >20%（未注入） | 2.4 GHz 拥挤 / USB3 干扰 / 板子离电脑太近（饱和） | 换信道 1/6/11；板子离硬盘/U 盘 30 cm 以上；距离拉到 2 m 再试 |
| 板子突然重启（`up` 归零） | USB 供电不足 | 换带供电的 USB hub / 5V 1A 充电头 |
| 采集到一半曲线不动 | 电脑睡眠 / 热点被关 | 电源计划设"高性能"、关闭睡眠；重跑该条 |
| Clumsy 没效果 | 过滤器没写对 / 没管理员运行 | 过滤器 `udp and udpport 9000`；管理员运行 |
| Python 被防火墙拦 | 首次运行没允许 | 允许"专用网络"访问；或临时关闭防火墙测试 |

---

## 七、明天的最低目标（时间不够就按这个砍）

| 优先级 | 采什么 | 时间 | 能得到什么 |
|---|---|---|---|
| **必须** | `good_desk` + `wall_5m` | 16 min | 证明"链路可观测、位置有差异" |
| **强烈建议** | 再加 `good_desk_2` + `wall_5m_2` + `outage_far` | 约 35 min | 能训第一版模型并评估误报 |
| 有余力 | 全套 9 条（含 2 条注入） | 约 57 min | 完整数据集，可写进报告 |

> 树莓派明天不用开机；摄像头的 UVC spike 等链路数据采完再做（那是另一条线）。
