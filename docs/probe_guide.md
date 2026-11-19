# 链路探针使用指南（ESP32-S3 + PC）

> 目标：**用真实 WiFi 采出第一条可复核的链路 trace**（RTT / 丢包 / RSSI / 吞吐），作为 PatroLink 的第一份实验证据，替换掉"我自己编的仿真 trace"。
> 时间：首次跑通约 40 分钟（烧录 10 分钟 + 测量 30 分钟）。

---

## 一、拓扑（两种都行，优先 A）

**A. PC 开热点，ESP32-S3 连上来**（推荐：PC 侧能直接看到板子 IP、抓包方便）

```
[PC]  开 Windows 移动热点（2.4 GHz！）  ← WiFi ←  [ESP32-S3 探针]
  │ 跑 pc/probe_link.py                    UDP 9000 / HTTP 80
  └ 记录 traces/*.csv + 图
```

**B. 手机热点，PC 与 ESP32-S3 都连它**（PC 热点开不了时用）

```
[手机热点] ← [PC] 记录
           ← [ESP32-S3]
```

> ⚠️ **ESP32-S3 只支持 2.4 GHz**。Windows 移动热点如果只开 5 GHz，板子连不上——在"移动热点 → 属性"里把频段改成 2.4 GHz；改不了就用手机热点。

---

## 二、烧录固件（10 分钟）

1. 打开 `firmware/esp32s3_probe/esp32s3_probe.ino`，**只改两行**：
   ```cpp
   #define WIFI_SSID "PatroLink-AP"   // 你的热点名
   #define WIFI_PASS "12345678"       // 热点密码
   ```
2. Arduino IDE 选板子：
   | 选项 | 值 |
   |---|---|
   | Board | **ESP32S3 Dev Module** |
   | USB CDC On Boot | **Enabled**（否则串口无输出） |
   | PSRAM | 有就 OPI PSRAM，没有就 Disabled |
   | Flash Size | 按板子（8MB / 16MB） |
   | Partition Scheme | Default 4MB with spiffs（够用） |
3. 板子插 **UART 口**（不是 USB 口）→ 选 COM 口 → 上传。
4. 打开串口监视器（115200），应看到：
   ```
   === PatroLink ESP32-S3 link probe v0.1 ===
   [wifi] connecting to PatroLink-AP....
   [wifi] OK ip=192.168.137.2 rssi=-46 ch=6
   [http] listening on 80, udp probe on 9000
   [hb] rssi=-46 ch=6 ip=192.168.137.2 heap=2xxxxx up=5s
   ```
   **把 IP 记下来**（后面 `--host` 用）。

> 编译已验证：ESP32 Arduino core 3.3.10，`esp32:esp32:esp32s3`，固件占 68% flash、14% RAM。

---

## 三、测量（30 分钟，这就是今天的验收）

```powershell
# 1) 先确认能通（把 IP 换成串口里看到的）
ping 192.168.137.2

# 2) 桌边：30 秒，20 Hz
python pc\probe_link.py --host 192.168.137.2 --duration 30 --label desk

# 3) 拿着板子到隔墙处（5 m），再跑一遍
python pc\probe_link.py --host 192.168.137.2 --duration 30 --label wall

# 4) 楼道尽头/门后（10 m+），再跑一遍
python pc\probe_link.py --host 192.168.137.2 --duration 30 --label corridor

# 5) 顺手测吞吐（可选，但很值）
python pc\probe_link.py --host 192.168.137.2 --duration 10 --label desk --sink-kb 64 --echo-kb 64
```

输出长这样（真实例子是假节点跑的，仅示范格式）：

```
──── 结果 ────
  发送 600 包 ｜ 收到 598 ｜ 丢包 0.3%
  RTT 中位数 24.3 ms ｜ P95 38.1 ms ｜ 最小 18.2 ｜ 最大 96.4
  抖动（相邻差中位数）1.9 ms
  RSSI 中位数 -52.0 dBm ｜ 范围 -61 ~ -44 dBm
  上行吞吐：64 KB / 120 ms → 4.37 Mbps
```

产物（`traces/` 下）：
- `probe_<label>_<时间>.csv`：逐包 `wall_ms, seq, rtt_ms, rssi_dbm, lost`
- `probe_<label>_<时间>.json`：汇总（中位数/P95/丢包/RSSI/吞吐）
- `probe_<label>_<时间>.png`：RTT + RSSI 两条曲线（需 matplotlib）

**W1 验收判据**：三个位置的 **RTT 中位数差 ≥ 10 ms** 或 **RSSI 差 ≥ 5 dBm** —— 说明"真实链路可观测"，这比任何仿真都值钱。

---

## 四、把结果接进日志契约（v0.2 计划字段）

| 探针产物 | 日志字段（v0.2 新增） |
|---|---|
| `rtt_ms` 中位数（每 1 s 窗口） | `link_probe_rtt_ms` |
| `rssi_dbm` | `link_rssi_dbm` |
| `lost` 的窗口比例 | `link_loss_rate`（替换仿真值） |
| `--sink-kb` 的吞吐 | `link_thr_mbps` |
| 位置标签 `label` | `link_zone`（desk/wall/corridor 映射为 good/occlusion/…） |

---

## 五、故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 串口没输出 | `USB CDC On Boot` 没开 / 插错口 | 改成 Enabled，插 UART 口；或换一根数据线（很多线只供电） |
| `[wifi] FAILED` 反复重连 | 热点是 5 GHz / 密码错 / 热点没开 | 热点改 2.4 GHz；确认密码 ≥8 位；手机热点替代 |
| 连上了但 `ping` 不通 | PC 防火墙拦 ICMP / 热点 AP 隔离 | 换手机热点；或允许 Python 通过防火墙 |
| 探针丢包 100% | 板子 IP 变了（DHCP） | 看串口打印的 IP 重跑；或在热点里给板子绑定固定 IP |
| 丢包 10%+ 但离得近 | 2.4 GHz 信道拥挤 / USB3 干扰 | 换信道（1/6/11）、板子离 USB3 硬盘远一点 |
| RTT 异常小（<5 ms） | 客户端与服务端在同一台机器（用了假节点） | 正常，假节点就是本机回环 |
| 实际发包率远低于 `--hz` | 每次发包都等回包（顺序探测），RTT 大时自然变慢 | 属正常；需要高采样率时把 `--hz` 设在 `1000/RTT` 以下 |
| 板子反复重启 | USB 供电不足 | 换带供电的 USB hub / 5V 1A 充电头 |

---

## 六、没有硬件时怎么自测

```powershell
# 终端 A：假节点（模拟"越走越差"的链路）
python tools\fake_esp32_probe.py --pattern degrade --ramp-s 20 --loss 0.01

# 终端 B：探针
python pc\probe_link.py --host 127.0.0.1 --udp-port 9000 --http-port 8080 --duration 10 --label fake
```

`--pattern good|degrade|oscillate` 可以造"链路由好变差再恢复"的剧本，用来先把 PC 侧、绘图、CSV 全部调通，再上真板子。

---

## 七、做完这一步，下一步是什么

1. **USB 摄像头 spike**（决定性岔路）：S3 的 UVC 能不能出帧？
   - 能 → 摄像头接板子，按 PPT 架构；
   - 3 天搞不定 → 摄像头接 PC/树莓派，S3 只做链路扰动节点。
2. 跑通"一张图检测 + 第一条 tick 日志"（用现成 `scripts/validate_log.py` 校验）。
3. 把探针 trace 换成日志里的 `link_*` 字段，调度器第一次读到**真实链路**。
