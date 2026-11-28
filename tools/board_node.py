#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""板端节点：跑在机器人那一侧的板子上（树莓派 5、STM32MP257 等任何 Linux 板子）。

只依赖 Python 标准库（3.8 以上），不绑定网卡型号：无线接口自动识别，读不到 RSSI 也照常工作。
Windows 上也能运行，用来在没有板子时自测。

做三件事：
    1) 回显 UDP 探测包（默认 9000 端口）。协议与 ESP32-S3 固件相同，回包里带本机 RSSI；
    2) 按笔记本的指令，向笔记本发送模拟的"帧"（整帧拆成 UDP 分片，收齐后由笔记本确认）；
    3) 按笔记本的指令，把本机的链路状态（RSSI、信道、速率、重传计数）实时发给笔记本。

采集数据全部落在笔记本上（见 pc_collect.py），板端只留一份备份日志。

用法（板子上只需要这一条命令，保持窗口开着）：
    python3 board_node.py
    python3 board_node.py --iface wlan0       # 自动识别不到无线接口时手动指定
    python3 board_node.py --no-rssi           # 板子上没有 iw，或者不需要 RSSI
    python3 board_node.py --selftest          # 只读几次链路状态并打印，不启动服务

Linux 上采集前建议关掉无线网卡的省电：sudo iw dev <接口名> set power_save off
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import platform
import random
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

PROBE_REQ = struct.Struct("<Iq")        # seq, t0_us —— 与 ESP32-S3 固件一致
PROBE_RSP = struct.Struct("<IqqqhH")    # seq, t0, t1, t2, rssi(int16), dl_lost(uint16)，32 字节，与固件的回包等长
FRAG_HDR = struct.Struct("!IHH")        # frame_id, frag_idx, n_frag
FRAG_SIZE = 1200                        # 每个 UDP 数据报的总字节数
NO_RSSI = -32768                        # 回包里表示"读不到 RSSI"
STATE_KEYS = ["rssi", "channel", "band", "tx_rate", "rx_rate", "signal_avg",
              "tx_retries", "tx_failed", "beacon_loss", "rx_bytes", "tx_bytes"]


# ---------------- 读链路状态 ----------------
def find_wireless_ifaces():
    """Linux：列出无线接口名；其它系统返回空列表。"""
    base = Path("/sys/class/net")
    names = []
    if base.is_dir():
        for p in sorted(base.iterdir()):
            if (p / "wireless").is_dir() or (p / "phy80211").is_dir():
                names.append(p.name)
    return names


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, timeout=3).stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _search(text, patterns):
    out = {}
    for key, pat in patterns.items():
        m = re.search(pat, text, re.M)
        if m:
            out[key] = m.group(1)
    return out


WIN_PATTERNS = {                      # netsh 的输出随系统语言变化，按中英文两种写法匹配
    "rssi": r"^\s*Rssi\s*:\s*(-?\d+)",
    "channel": r"^\s*(?:通道|Channel)\s*:\s*(\d+)",
    "band": r"^\s*(?:波段|Band)\s*:\s*(\S+)",
    "rx_rate": r"^\s*(?:接收速率|Receive rate)\s*\(Mbps\)\s*:\s*([\d.]+)",
    "tx_rate": r"^\s*(?:传输速率|Transmit rate)\s*\(Mbps\)\s*:\s*([\d.]+)",
    "bssid": r"^\s*(?:AP )?BSSID\s*:\s*([0-9a-fA-F:]{17})",
}


def read_link_windows():
    """Windows：读一次本机无线网卡的状态；未连接时返回 None。只用于自测。"""
    try:
        raw = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("gbk", errors="replace")
    out = _search(text, WIN_PATTERNS)
    return out if "rssi" in out else None


IW_LINK = {
    "bssid": r"^Connected to ([0-9a-fA-F:]{17})",
    "rssi": r"^\s*signal:\s*(-?\d+)\s*dBm",
    "tx_rate": r"^\s*tx bitrate:\s*([\d.]+)",
    "rx_rate": r"^\s*rx bitrate:\s*([\d.]+)",
    "rx_bytes": r"^\s*RX:\s*(\d+)",
    "tx_bytes": r"^\s*TX:\s*(\d+)",
}
IW_STATION = {
    "signal_avg": r"^\s*signal avg:\s*(-?\d+)\s*dBm",
    "tx_retries": r"^\s*tx retries:\s*(\d+)",
    "tx_failed": r"^\s*tx failed:\s*(\d+)",
    "beacon_loss": r"^\s*beacon loss:\s*(\d+)",
}


def freq_to_channel(freq_mhz):
    f = int(freq_mhz)
    if f == 2484:
        return 14
    if 2412 <= f <= 2472:
        return (f - 2407) // 5
    if f >= 5000:
        return (f - 5000) // 5
    return None


class LinuxLink:
    """用 iw 读无线接口状态。iw 的输出固定是英文。"""

    def __init__(self, iface):
        self.iface = iface
        self.station, self.t_station = "", 0.0

    def __call__(self):
        link = _run(["iw", "dev", self.iface, "link"])
        if "Not connected" in link or not link:
            return None
        now = time.perf_counter()
        if now - self.t_station >= 1.0:        # 重传等计数器变化慢，1 秒读一次就够
            self.station = _run(["iw", "dev", self.iface, "station", "dump"])
            self.t_station = now
        out = _search(link, IW_LINK)
        if "rssi" not in out:
            return None
        out.update(_search(self.station, IW_STATION))
        for key in ("rx_bytes", "tx_bytes"):        # 有的驱动不给收发字节数，改从系统统计里读
            if key not in out:
                try:
                    with open("/sys/class/net/%s/statistics/%s" % (self.iface, key)) as f:
                        out[key] = f.read().strip()
                except OSError:
                    pass
        m = re.search(r"^\s*freq:\s*(\d+)", link, re.M)
        if m:
            ch = freq_to_channel(m.group(1))
            out["channel"] = str(ch) if ch else m.group(1)
            out["band"] = "2.4 GHz" if int(m.group(1)) < 3000 else "5 GHz"
        return out


def pick_link_reader(args):
    """返回 (读取函数 或 None, 说明)。读不到不影响探测，只是回包里没有 RSSI。"""
    if args.no_rssi:
        return None, "已用 --no-rssi 关闭"
    if sys.platform.startswith("linux"):
        ifaces = [args.iface] if args.iface else find_wireless_ifaces()
        if not ifaces:
            return None, "没有找到无线接口（可用 --iface 指定）"
        if not _run(["iw", "--version"]):
            return None, "没有 iw 命令（用系统的包管理器装上，例如 apt install iw），或者加 --no-rssi"
        return LinuxLink(ifaces[0]), "接口 " + ifaces[0]
    if sys.platform.startswith("win"):
        return read_link_windows, "netsh（自测用）"
    return None, "这个系统上不读取链路状态"


class LinkMonitor(threading.Thread):
    """后台定期读链路状态，并留一份备份日志。"""

    def __init__(self, reader, refresh_ms, log_dir):
        super().__init__(daemon=True)
        self.reader, self.period, self.log_dir = reader, max(20, refresh_ms) / 1000.0, Path(log_dir)
        self.state, self.lock = None, threading.Lock()

    def run(self):
        ap_id, last_bssid = 0, None
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_dir / ("boardlink_%s.csv" % datetime.now().strftime("%Y%m%d_%H%M%S"))
        with path.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["wall_ms"] + STATE_KEYS + ["ap_id"])
            while True:
                t0 = time.perf_counter()
                try:
                    link = self.reader() if self.reader else None
                except Exception:
                    link = None
                if link:
                    bssid = link.get("bssid")
                    if last_bssid is not None and bssid != last_bssid:
                        ap_id += 1                     # 换了一个 AP
                    last_bssid = bssid or last_bssid
                    state = {k: link[k] for k in STATE_KEYS if link.get(k) is not None}
                    state["ap_id"] = ap_id
                    with self.lock:
                        self.state = state
                    w.writerow([int(time.time() * 1000)] + [state.get(k, "") for k in STATE_KEYS] + [ap_id])
                    f.flush()
                time.sleep(max(0.0, self.period - (time.perf_counter() - t0)))

    def snapshot(self):
        with self.lock:
            return dict(self.state) if self.state else None

    def rssi(self):
        state = self.snapshot()
        try:
            return int(state["rssi"])
        except (TypeError, KeyError, ValueError):
            return NO_RSSI


# ---------------- 回显探测包 ----------------
def timestamp_consts():
    """返回 (SO_TIMESTAMPNS, SCM_TIMESTAMPNS)。

    有的 Linux 系统（例如 Alpine）的 Python 里没有这两个常量，所以缺失时直接用数值：
    在 x86_64、aarch64 和 32 位 ARM 上它们的取值都是 35。
    """
    return getattr(socket, "SO_TIMESTAMPNS", 35), getattr(socket, "SCM_TIMESTAMPNS", 35)


def enable_kernel_timestamps(sock):
    """Linux：让内核在收包时刻打时间戳。板子忙时进程可能被推迟调度，这样量到的时延不含调度延迟。"""
    if not sys.platform.startswith("linux"):
        return False
    try:
        so_ts, _ = timestamp_consts()
        sock.setsockopt(socket.SOL_SOCKET, so_ts, 1)
        socket.CMSG_SPACE(32)
        return True
    except (AttributeError, OSError, OverflowError):
        return False


def kernel_time_us(anc):
    """从 recvmsg 的辅助数据里取内核收包时间戳（CLOCK_REALTIME，微秒）；没有则返回 None。"""
    _, scm_ts = timestamp_consts()
    for level, ctype, cdata in anc:
        if level == socket.SOL_SOCKET and ctype == scm_ts and len(cdata) in (8, 16):
            sec, nsec = struct.unpack("qq" if len(cdata) == 16 else "ii", cdata)
            return sec * 1000000 + nsec // 1000
    return None


def set_low_latency(sock):
    """尽量让探测包走低时延队列：链路上有视频大流量时，探测回包不必排在后面。

    驱动不支持时没有任何影响。
    """
    for level, name, value in ((socket.SOL_SOCKET, "SO_PRIORITY", 6),        # 6 = 语音级别的优先级
                               (socket.IPPROTO_IP, "IP_TOS", 0xB8)):          # DSCP 加速转发
        try:
            sock.setsockopt(level, getattr(socket, name), value)
        except (OSError, AttributeError):
            pass


class ProbeServer(threading.Thread):
    def __init__(self, bind, port, monitor, args):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        self.sock.bind((bind, port))
        self.sock.settimeout(0.1)
        self.kernel_ts = enable_kernel_timestamps(self.sock)
        if not getattr(args, "no_probe_qos", False):
            set_low_latency(self.sock)
        self.monitor, self.args = monitor, args
        self.received = set()
        self.n_req = self.n_rsp = self.n_send_fail = 0
        self.max_gap = 0.0
        self.n_req = self.n_rsp = self.n_send_fail = 0
        self.rng = random.Random(1)

    def reset(self):
        self.received.clear()

    def _send(self, rsp, addr):
        try:
            self.sock.sendto(rsp, addr)
            self.n_rsp += 1
        except OSError as exc:
            self.n_send_fail += 1
            if self.n_send_fail <= 3:
                print("[node] 回包没发出去（第 %d 次）：%s" % (self.n_send_fail, exc), flush=True)

    def _woke(self, last_wake):
        wake = time.perf_counter()
        self.max_gap = max(self.max_gap, wake - last_wake)   # 这个循环相邻两次醒来之间最长的间隔
        return wake

    def run(self):
        last_wake = time.perf_counter()
        while True:
            try:
                if self.kernel_ts:
                    data, anc, _flags, addr = self.sock.recvmsg(2048, 64)
                else:
                    data, addr = self.sock.recvfrom(2048)
                    anc = []
            except socket.timeout:
                last_wake = self._woke(last_wake)
                continue
            except OSError:                       # 上一条回包没送达时系统可能报错，这是暂时的
                time.sleep(0.01)
                last_wake = self._woke(last_wake)
                continue
            last_wake = self._woke(last_wake)
            if len(data) < PROBE_REQ.size:
                continue
            seq, t0 = PROBE_REQ.unpack_from(data)
            self.n_req += 1
            if self.args.drop and self.rng.random() < self.args.drop:
                if self.rng.random() < 0.5:
                    continue                         # 仅自测：当作请求没送到板子（去程丢包）
                self.received.add(seq)
                continue                             # 仅自测：收到了但不回（回程丢包）
            self.received.add(seq)
            t1 = kernel_time_us(anc)
            if t1 is not None:
                t2 = time.time_ns() // 1000               # 内核时间戳是 CLOCK_REALTIME，t2 用同一个时钟
            else:
                t1 = t2 = time.perf_counter_ns() // 1000
            rsp = PROBE_RSP.pack(seq, t0, t1, t2, self.monitor.rssi(), max(0, seq - len(self.received)))
            if self.args.late_every and self.n_req % self.args.late_every == 0:
                threading.Timer(self.args.late_ms / 1000.0, self._send, args=(rsp, addr)).start()
            else:
                self._send(rsp, addr)


# ---------------- 发送模拟帧 ----------------
class FrameSender(threading.Thread):
    def __init__(self, target, kb, fps, seconds, log_dir, drop=0.0):
        super().__init__(daemon=True)
        self.target, self.kb, self.fps, self.seconds, self.log_dir = target, kb, fps, seconds, Path(log_dir)
        self.drop, self.rng = drop, random.Random(2)
        self.summary, self._stop_evt = None, threading.Event()

    def stop(self):
        self._stop_evt.set()

    def run(self):
        payload = os.urandom(FRAG_SIZE - FRAG_HDR.size)
        n_frag = max(1, math.ceil(self.kb * 1024 / len(payload)))
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(self.target)
        acks, pending, done, fid, dropped = {}, [], [], 0, 0

        def drain():
            sock.setblocking(False)
            while True:
                try:
                    data, _ = sock.recvfrom(16)
                except OSError:
                    break
                if len(data) >= 4:
                    acks[struct.unpack("!I", data)[0]] = time.perf_counter()

        def settle(wait_s):
            """把已确认或已超时的帧从 pending 挪到 done。wait_s 为 None 时只挪已确认的。"""
            still = []
            for row in pending:
                if row[6] is None:
                    if row[0] in acks:
                        row[6] = (acks[row[0]] - row[2]) * 1000.0
                    elif wait_s is not None and time.perf_counter() - row[2] >= wait_s:
                        pass                        # 超时：记为未确认
                    else:
                        still.append(row)
                        continue
                done.append(row)
            return still

        interval, t_next = 1.0 / self.fps, time.perf_counter()
        t_end = t_next + (self.seconds if self.seconds > 0 else 1e18)
        while not self._stop_evt.is_set() and time.perf_counter() < t_end:
            fid += 1
            wall, t0, fails, lost_test = int(time.time() * 1000), time.perf_counter(), 0, 0
            sock.setblocking(True)                  # 发送缓冲区满时阻塞，和应用一次性交出一帧一致
            for idx in range(n_frag):
                if self.drop and self.rng.random() < self.drop:
                    lost_test += 1                  # 仅自测：这一片故意不发
                    continue
                try:
                    sock.send(FRAG_HDR.pack(fid, idx, n_frag) + payload)
                except OSError:
                    fails += 1
            dropped += lost_test
            pending.append([fid, wall, t0, (time.perf_counter() - t0) * 1000.0, fails, lost_test, None])
            t_next += interval
            while not self._stop_evt.is_set():      # 等到下一帧，期间每 2 ms 读一次确认
                left = t_next - time.perf_counter()
                if left <= 0:
                    break
                time.sleep(min(left, 0.002))
                drain()
            drain()
            pending = settle(1.0)
        if not self._stop_evt.is_set():             # 按时长自然结束：再等 1 秒收尾
            deadline = time.perf_counter() + 1.0
            while time.perf_counter() < deadline and pending:
                time.sleep(0.01)
                drain()
                pending = settle(None)
        done.extend(pending)                        # 停止时还没确认的帧，记为未确认
        sock.close()

        done.sort(key=lambda r: r[0])
        ack_ms = sorted(r[6] for r in done if r[6] is not None)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        with (self.log_dir / ("frames_send_%s.csv" % datetime.now().strftime("%Y%m%d_%H%M%S"))).open(
                "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["first_wall_ms", "frame_id", "kb", "n_frag", "burst_ms", "send_fail", "dropped", "ack_delay_ms"])
            for r in done:
                w.writerow([r[1], r[0], self.kb, n_frag, "%.2f" % r[3], r[4], r[5], "" if r[6] is None else "%.2f" % r[6]])
        self.summary = {
            "frames_sent": fid, "frames_acked": len(ack_ms), "n_frag": n_frag,
            "send_fail": sum(r[4] for r in done), "dropped_test": dropped,
            "ack_ms_median": round(ack_ms[len(ack_ms) // 2], 2) if ack_ms else None,
            "ack_ms_p95": round(ack_ms[int(0.95 * (len(ack_ms) - 1))], 2) if ack_ms else None,
        }


def set_system_clock(epoch_ms):
    """把系统时钟设成给定的 Unix 毫秒时间。板子一般没有电池时钟，上电后的时间不可信。

    需要 root 权限。成功返回 None，失败返回原因。
    """
    if not sys.platform.startswith("linux"):
        return "只支持 Linux"
    try:
        import ctypes

        class Timespec(ctypes.Structure):
            _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]

        ts = Timespec(int(epoch_ms // 1000), int((epoch_ms % 1000) * 1000000))
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.clock_settime(0, ctypes.byref(ts)) != 0:      # 0 = CLOCK_REALTIME
            err = ctypes.get_errno()
            return "%s（errno %d）" % (os.strerror(err), err)
    except Exception as exc:
        return str(exc)
    return None


# ---------------- 节点与控制通道 ----------------
class Node:
    def __init__(self, args, monitor, probe, link_source):
        self.args, self.monitor, self.probe, self.link_source = args, monitor, probe, link_source
        self.peer = None                           # 最近一个下指令的笔记本 (ip, port)
        self.frames = None
        self.stream_on, self.stream_period, self.t_stream = False, 0.2, 0.0

    def info(self):
        return {"hostname": platform.node(), "system": platform.platform(),
                "python": platform.python_version(), "has_rssi": self.monitor.reader is not None,
                "link_source": self.link_source, "kernel_ts": self.probe.kernel_ts,
                "time_ms": int(time.time() * 1000)}

    def stream_tick(self, sock):
        if not self.stream_on or self.peer is None:
            return
        now = time.perf_counter()
        if now - self.t_stream < self.stream_period:
            return
        state = self.monitor.snapshot() or {}
        self.t_stream = now
        msg = {"t": int(time.time() * 1000), "l": [state.get(k, "") for k in STATE_KEYS] + [state.get("ap_id", "")]}
        try:
            sock.sendto(json.dumps(msg, separators=(",", ":")).encode("utf-8"), self.peer)
        except OSError:
            pass

    def start_frames(self, msg):
        self.stop_frames()
        if self.peer is None:
            return
        kb, fps = float(msg.get("kb", 50)), float(msg.get("fps", 10))
        seconds = float(msg.get("seconds", 0))
        self.frames = FrameSender((self.peer[0], int(msg.get("port", 9001))), kb, fps, seconds,
                                  self.args.out, self.args.drop)
        self.frames.start()
        print("[ctrl] 开始发帧：%g KB × %g 帧/秒 → %s:%s" % (kb, fps, self.peer[0], msg.get("port", 9001)), flush=True)

    def stop_frames(self):
        summary = None
        if self.frames is not None:
            self.frames.stop()
            self.frames.join(3.0)
            summary = self.frames.summary
            self.frames = None
            print("[ctrl] 停止发帧", flush=True)
        return summary

    def handle(self, msg):
        cmd = msg.get("cmd")
        rid = msg.get("id")
        if cmd == "hello":
            return {"id": rid, "ok": True, "node": self.info(), "link": self.monitor.snapshot()}
        if cmd == "reset":
            self.probe.reset()
            return {"id": rid, "ok": True}
        if cmd == "settime":
            err = set_system_clock(msg.get("epoch_ms"))
            return {"id": rid, "ok": err is None, "error": err, "time_ms": int(time.time() * 1000)}
        if cmd == "stream":
            self.stream_on = bool(msg.get("on"))
            self.stream_period = max(0.05, float(msg.get("period_ms", 200)) / 1000.0)
            return {"id": rid, "ok": True, "stream": self.stream_on}
        if cmd == "frames":
            summary = self.start_frames(msg) if msg.get("on") else self.stop_frames()
            return {"id": rid, "ok": True, "frames": summary}
        if cmd == "status":
            return {"id": rid, "ok": True, "probe_req": self.probe.n_req, "probe_rsp": self.probe.n_rsp,
                    "probe_send_fail": self.probe.n_send_fail,
                    "probe_max_stall_ms": round(max(0.0, self.probe.max_gap - 0.1) * 1000.0)}   # 空闲时每 0.1 秒醒一次，超出部分是被卡住的时间
        return {"id": rid, "ok": False, "error": "unknown command: %s" % cmd}


class CtrlServer(threading.Thread):
    def __init__(self, node, bind, port):
        super().__init__(daemon=True)
        self.node = node
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((bind, port))
        self.sock.settimeout(0.05)

    def run(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                self.node.stream_tick(self.sock)
                continue
            except OSError:                       # 上一条回复/状态流没送达时系统可能报错，这是暂时的
                self.node.stream_tick(self.sock)
                time.sleep(0.01)
                continue
            try:
                msg = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(msg, dict) or not msg.get("cmd"):
                continue
            self.node.peer = addr                    # 回复和链路状态流都发回这个地址
            reply = self.node.handle(msg)
            try:
                self.sock.sendto(json.dumps(reply, ensure_ascii=False).encode("utf-8"), addr)
            except OSError:
                pass


def selftest(args):
    reader, reason = pick_link_reader(args)
    print("系统：%s ｜ Python %s" % (platform.platform(), platform.python_version()))
    print("链路状态来源：%s" % reason)
    for i in range(3):
        t0 = time.perf_counter()
        try:
            link = reader() if reader else None
        except Exception as exc:                    # 读取出错时给出原因，不影响探测
            link, reason = None, "%s（%s）" % (reason, exc)
        dt = (time.perf_counter() - t0) * 1000
        print("第 %d 次：%4.0f ms ｜ %s" % (i + 1, dt, {k: v for k, v in (link or {}).items() if k != "bssid"}))
    if reader and not link:
        print("没读到链路状态：%s。不影响探测，只是回包里没有 RSSI。" % reason)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="板端节点：回显探测包、按指令发帧、上报链路状态")
    ap.add_argument("--bind", default="0.0.0.0", help="监听地址；只在笔记本本机自测时用 127.0.0.1")
    ap.add_argument("--probe-port", type=int, default=9000)
    ap.add_argument("--ctrl-port", type=int, default=9002)
    ap.add_argument("--iface", default="", help="无线接口名；不填则自动识别")
    ap.add_argument("--no-rssi", action="store_true", help="不读链路状态（回包里 RSSI 记为空）")
    ap.add_argument("--no-probe-qos", action="store_true",
                    help="不给探测回包打低时延标记（默认打上，链路上有大流量时可能有用）")
    ap.add_argument("--refresh-ms", type=int, default=0, help="读链路状态的间隔（毫秒）；0 取默认值")
    ap.add_argument("--out", default="traces", help="板端备份日志的目录")
    ap.add_argument("--selftest", action="store_true", help="只读几次链路状态并打印，不启动服务")
    ap.add_argument("--drop", type=float, default=0.0, help="自测用：随机丢弃这个比例的探测请求和帧分片")
    ap.add_argument("--late-every", type=int, default=0, help="自测用：每多少个探测请求让回包迟到一次")
    ap.add_argument("--late-ms", type=float, default=350.0, help="自测用：回包迟到多少毫秒")
    args = ap.parse_args(argv)
    if not args.refresh_ms:
        args.refresh_ms = 200 if sys.platform.startswith("linux") else 250
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)   # 输出重定向到日志文件时也一行一行及时写出
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if args.selftest:
        selftest(args)
        return 0

    reader, reason = pick_link_reader(args)
    monitor = LinkMonitor(reader, args.refresh_ms, args.out)
    monitor.start()
    try:
        probe = ProbeServer(args.bind, args.probe_port, monitor, args)
        ctrl = CtrlServer(Node(args, monitor, probe, reason), args.bind, args.ctrl_port)
    except OSError as exc:
        print("端口被占用或无法监听：%s。通常是上一个节点还在运行：先执行 pkill -f board_node.py，"
              "或者用 --probe-port / --ctrl-port 换端口。" % exc)
        return 1
    probe.start()
    ctrl.start()
    print("=== PatroLink 板端节点 ===")
    print("主机：%s ｜ 系统：%s ｜ Python %s" % (platform.node(), platform.platform(), platform.python_version()))
    print("探测回显：UDP %d ｜ 控制：UDP %d" % (args.probe_port, args.ctrl_port))
    print("链路状态：%s ｜ 每 %d ms 读一次 ｜ 内核收包时间戳：%s"
          % (reason, args.refresh_ms, "开" if probe.kernel_ts else "关"))
    print("备份日志目录：%s" % Path(args.out).resolve())
    print("保持这个窗口开着。在笔记本上运行：python pc_collect.py doctor --board <本机 IP>")
    n, t_print = 0, time.perf_counter()
    try:
        while True:
            time.sleep(1.0)
            if time.perf_counter() - t_print >= 5:
                t_print = time.perf_counter()
                rssi = monitor.rssi()
                print("[node] 已回显 %d ｜ RSSI %s" % (probe.n_rsp, "空" if rssi == NO_RSSI else "%d dBm" % rssi),
                      flush=True)
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    raise SystemExit(main())
