# traces/ —— 链路探针产物

这个目录放 `pc/probe_link.py` 跑出来的东西，**默认不进 git**（见 `.gitignore`）。

## 跑步命令

```powershell
# 无硬件先自测
python tools\fake_esp32_probe.py --pattern degrade
python pc\probe_link.py --host 127.0.0.1 --udp-port 9000 --http-port 8080 --duration 10 --label fake

# 真实测量（IP 换成串口打印的 IP）
python pc\probe_link.py --host 192.168.137.2 --duration 30 --label desk
python pc\probe_link.py --host 192.168.137.2 --duration 30 --label wall
python pc\probe_link.py --host 192.168.137.2 --duration 30 --label corridor
```

## 产物格式

| 文件 | 内容 |
|---|---|
| `probe_<label>_<时间>.csv` | 逐包：`wall_ms, seq, rtt_ms, rssi_dbm, lost` |
| `probe_<label>_<时间>.json` | 汇总：中位数/P95/丢包率/RSSI/吞吐 + 元信息（host、label、hz） |
| `probe_<label>_<时间>.png` | RTT 与 RSSI 两条曲线（需 matplotlib） |

## 要提交的"证据 trace"

如果要把某条 trace 作为报告证据，**不要直接 commit 原始 csv**，而是：

1. 在汇报/文档里引用 `probe_<label>_<ts>.json` 的关键数字（RTT 中位数、P95、丢包率、RSSI）；
2. 需要留档时，把该次测量裁剪成 10–20 秒的代表性片段，放到 `docs/evidence/` 并写明采集条件（距离、遮挡、信道、时间）。

判断标准：**看的人能不能复现**——写清楚设备、距离、位置、热点频段，比 trace 本身更重要。
