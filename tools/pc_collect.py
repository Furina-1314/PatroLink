#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""笔记本侧采集器：向板端节点（tools/board_node.py）发探测包、收模拟帧、收链路状态，并把数据全部落盘。

只依赖 Python 标准库。产出的 probe_<标签>_<时间>.csv / .json 与 pc/probe_link.py 的格式兼容，
可以直接交给 ml/build_dataset.py 和面板。采集数据全部落在笔记本上，不需要从板子拷贝。

用法：
    # 0) 先自检：板子和笔记本之间的链路通不通，不通时给出原因
    python pc_collect.py doctor --board <板子 IP>

    # 1) 采一条只带探测的 trace
    python pc_collect.py run --board <板子 IP> --label A_fwd_1 --duration 90 --route A --direction fwd --speed 1.0

    # 2) 采一条带载的 trace：探测照跑，同时板子按"约 4 Mbps 的视频码流"发模拟包
    python pc_collect.py run --board <板子 IP> --label A_load --duration 60 --load 4mbps

    # 3) 按计划逐条采（每条开始前按回车；计划格式与 pc/collect_dataset.py 相同，可加 load / repeat 等字段）
    python pc_collect.py plan --board <板子 IP> --plan plans/collect_plan.json

探测的发送和接收是分开的：发送按固定节拍，接收按序号配对。回包在 --timeout 秒内没到就记为丢包，
之后到了会另记在 late_rtt_ms 里，不影响后面的探测。
计时一律用 time.perf_counter()：Windows 上 Python 3.12 及更早版本的 time.monotonic() 只有约 15.6 ms 的
分辨率，量不了毫秒级的时延；perf_counter 在各版本上都是高分辨率的。
这些旧版本的 time.sleep() 也不准（受系统定时器限制），程序启动时会把定时器精度提到 1 ms，退出时恢复。

端口：探测走板端的 UDP 9000，控制走板端的 UDP 9002，帧发到笔记本的 UDP 9001。
笔记本只需要为帧放行入站 UDP 9001（首次运行时在防火墙提示里允许 Python，专用和公用都勾上）。
"""
from __future__ import annotations

import argparse
import atexit
import csv
import json
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

PROBE_REQ = struct.Struct("<Iq")        # seq, t0_us —— 与板端节点、ESP32-S3 固件一致
PROBE_RSP = struct.Struct("<IqqqhH")    # seq, t0, t1, t2, rssi, dl_lost
FRAG_HDR = struct.Struct("!IHH")        # frame_id, frag_idx, n_frag
NO_RSSI = -32768                        # 板端读不到 RSSI 时的回包值
FRAG_SIZE = 1200                        # 板端发帧时每个 UDP 包的字节数
STATE_KEYS = ["rssi", "channel", "band", "tx_rate", "rx_rate", "signal_avg",
              "tx_retries", "tx_failed", "beacon_loss", "rx_bytes", "tx_bytes"]
PROBE_COLS = ["wall_ms", "seq", "rtt_ms", "rssi_dbm", "lost", "late_rtt_ms", "dl_lost", "send_gap_ms"]
FRAME_COLS = ["first_wall_ms", "frame_id", "n_frag", "n_recv", "n_lost", "bytes_recv", "complete", "span_ms"]
MANIFEST_COLS = [
    "started_at", "label", "csv", "json", "duration_s", "distance_m", "obstacle",
    "channel", "hotspot", "inject", "note", "sent", "received", "loss_rate",
    "rtt_median", "rtt_p95", "rssi_median",
]


def pct(values, q):
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def rnd(x, nd=2):
    return None if x is None else round(x, nd)


def parse_load(text):
    """解析 --load，返回 (每帧 KB, 每秒帧数)；空 → (None, None)。

    '4mbps'            约 4 Mbps 的平滑码流，每包 1200 字节（接近 H.264 视频流的形态）
    '50x10'            每帧 50 KB、每秒 10 帧
    """
    if not text:
        return None, None
    text = str(text)
    m = re.match(r"^\s*([\d.]+)\s*mbps\s*$", text, re.I)
    if m:
        return FRAG_SIZE / 1024.0, float(m.group(1)) * 1e6 / 8.0 / FRAG_SIZE
    m = re.match(r"^\s*([\d.]+)\s*[xX*]\s*([\d.]+)\s*$", text)
    if not m:
        raise SystemExit("--load 的写法：码率，例如 4mbps；或者 每帧KBx每秒帧数，例如 50x10")
    return float(m.group(1)), float(m.group(2))


def load_text(kb, fps):
    """把负载写成人能读的形式。"""
    if abs(kb * 1024 - FRAG_SIZE) < 1 and fps >= 20:
        return "约 %g Mbps" % (kb * 1024 * fps * 8 / 1e6)
    return "%g KB × %g 帧/秒" % (kb, fps)


def sleep_until(t):
    """睡到 perf_counter 时刻 t：先粗睡，最后 2 ms 忙等，保证发送节拍稳定。"""
    left = t - time.perf_counter()
    if left > 0.002:
        time.sleep(left - 0.002)
    while time.perf_counter() < t:
        pass


def summarize(rows):
    ok = [r for r in rows if r[4] == 0]
    rtts = [float(r[2]) for r in ok]
    rssis = [float(r[3]) for r in rows if r[3] != ""]
    gaps = [float(r[7]) for r in rows if r[7] != ""]
    jit = [abs(rtts[i] - rtts[i - 1]) for i in range(1, len(rtts))]
    dl = [int(r[6]) for r in rows if r[6] not in ("", None)]
    gmed = pct(gaps, 0.5) if gaps else None
    return {
        "sent": len(rows), "received": len(ok),
        "loss_rate": round(1 - len(ok) / max(1, len(rows)), 4),
        "late_replies": len([r for r in rows if r[4] == 1 and r[5] != ""]),
        "dl_lost_total": max(dl) if dl else None,
        "rtt_ms": {"median": rnd(pct(rtts, 0.5)), "p95": rnd(pct(rtts, 0.95)),
                   "min": rnd(min(rtts) if rtts else None), "max": rnd(max(rtts) if rtts else None),
                   "jitter_median": rnd(pct(jit, 0.5))},
        "rssi_dbm": {"median": rnd(pct(rssis, 0.5), 1), "min": rnd(min(rssis) if rssis else None, 1),
                     "max": rnd(max(rssis) if rssis else None, 1)},
        "send_gap_ms": {"median": rnd(gmed), "p99": rnd(pct(gaps, 0.99)),
                        "off_cadence_share": round(sum(abs(g - gmed) > 5 for g in gaps) / max(1, len(gaps)), 4)},
    }


# ---------------- 探测：固定节拍发送，按序号配对接收 ----------------
class Probe:
    mark_probes = True                    # 是否给探测包打低时延标记（--no-probe-qos 关闭）

    def __init__(self, host, port, hz, timeout, late_timeout):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.connect((host, port))
        if Probe.mark_probes:
            try:
                self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0xB8)   # DSCP 加速转发
            except OSError:
                pass
        self.sock.settimeout(0.05)
        self.interval, self.timeout, self.late_timeout = 1.0 / hz, timeout, late_timeout
        self.pending, self.results = {}, {}     # pending: seq -> [t0_us, wall_ms, send_perf_counter, gap_ms]
        self.n_send_fail = self.n_replies = self.n_unmatched = 0
        self.max_gap = 0.0
        self.lock = threading.Lock()
        self.sending, self.closed = threading.Event(), threading.Event()
        self.sending.set()
        self.threads = [threading.Thread(target=self._send_loop, daemon=True),
                        threading.Thread(target=self._recv_loop, daemon=True)]

    def start(self):
        for t in self.threads:
            t.start()

    def stop(self):
        self.sending.clear()
        if self.threads[0].ident is None:         # 还没开始（比如倒计时时按了 Ctrl+C）
            self.sock.close()
            return
        self.threads[0].join(2.0)
        time.sleep(self.late_timeout)            # 给迟到的回包留时间
        self.closed.set()
        self.threads[1].join(2.0)
        self.sock.close()
        with self.lock:                          # 始终没有回包的，记为丢包
            left = sorted(self.pending.items())
            self.pending.clear()
        for seq, e in left:
            self.results[seq] = [e[1], seq, "", "", 1, "", "", e[3]]

    def rows(self):
        with self.lock:
            keys = sorted(self.results)
        return [self.results[k] for k in keys]

    def _send_loop(self):
        seq, t_next, t_last = 0, time.perf_counter(), None
        while self.sending.is_set():
            seq += 1
            t0 = time.perf_counter_ns() // 1000
            sent = time.perf_counter()
            gap = "" if t_last is None else round((t0 - t_last) / 1000.0, 2)
            t_last = t0
            with self.lock:                      # 先登记再发送：回包可能在 send 返回之前就到
                self.pending[seq] = [t0, int(time.time() * 1000), sent, gap]
            try:
                self.sock.send(PROBE_REQ.pack(seq, t0))
            except OSError:
                self.n_send_fail += 1
                with self.lock:
                    e = self.pending.pop(seq, None)
                if e is not None:
                    self.results[seq] = [e[1], seq, "", "", 1, "", "", e[3]]
            t_next += self.interval
            self._sweep()
            sleep_until(t_next)

    def _sweep(self):
        now = time.perf_counter()
        with self.lock:
            expired = [s for s, e in self.pending.items() if now - e[2] >= self.late_timeout]
            for s in expired:                    # 超过 late_timeout 仍未回：记为丢包
                e = self.pending.pop(s)
                self.results[s] = [e[1], s, "", "", 1, "", "", e[3]]

    def _recv_loop(self):
        last_wake = time.perf_counter()
        while not self.closed.is_set():
            try:
                data = self.sock.recv(64)
            except socket.timeout:
                data = None
            except OSError:                      # 对端没在监听时系统会报错：稍等后继续
                time.sleep(0.01)
                continue
            wake = time.perf_counter()
            self.max_gap = max(self.max_gap, wake - last_wake)   # 这个循环被卡住的最长时间
            last_wake = wake
            if data is None:
                continue
            t3 = time.perf_counter_ns() // 1000
            self.n_replies += 1
            if len(data) < PROBE_RSP.size:
                continue
            r_seq, r_t0, t1, t2, rssi, dl_lost = PROBE_RSP.unpack_from(data)
            rtt = ((t3 - r_t0) - (t2 - t1)) / 1000.0
            now = time.perf_counter()
            with self.lock:
                e = self.pending.pop(r_seq, None)
            if e is None:                        # 已经结过账，或者是重复的回包
                self.n_unmatched += 1
                continue
            late = now - e[2] > self.timeout
            rssi_s = "" if rssi == NO_RSSI else rssi
            if late:
                self.results[r_seq] = [e[1], r_seq, "", rssi_s, 1, "%.3f" % rtt, dl_lost, e[3]]
            else:
                self.results[r_seq] = [e[1], r_seq, "%.3f" % rtt, rssi_s, 0, "", dl_lost, e[3]]


# ---------------- 收帧 ----------------
class Sink(threading.Thread):
    def __init__(self, bind, port):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
        self.sock.bind((bind, port))
        self.sock.settimeout(0.05)
        self.frames, self.done_ids, self.rows = {}, set(), []
        self.n_frames = self.n_ok = self.n_lost_frag = 0
        self.closed = threading.Event()

    def _finish(self, fid):
        fr = self.frames.pop(fid)                # fr: [分片序号集合, 首片时刻, 末片时刻, 字节数, 分片总数, 地址, 首片墙钟]
        ok = len(fr[0]) == fr[4]
        if ok:
            try:
                self.sock.sendto(struct.pack("!I", fid), fr[5])
            except OSError:
                pass
        self.done_ids.add(fid)
        self.n_frames += 1
        self.n_ok += ok
        self.n_lost_frag += fr[4] - len(fr[0])
        self.rows.append([fr[6], fid, fr[4], len(fr[0]), fr[4] - len(fr[0]), fr[3], int(ok),
                          round((fr[2] - fr[1]) * 1000.0, 2)])

    def run(self):
        while not self.closed.is_set():
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                data = None
            except OSError:                      # 对端已经关掉端口时系统会报错：这一类是暂时的，继续收
                time.sleep(0.01)
                continue
            now = time.perf_counter()
            if data and len(data) >= FRAG_HDR.size:
                fid, idx, n_frag = FRAG_HDR.unpack_from(data)
                if fid not in self.done_ids:
                    fr = self.frames.get(fid)
                    if fr is None:
                        fr = self.frames[fid] = [set(), now, now, 0, n_frag, addr, int(time.time() * 1000)]
                    fr[0].add(idx)
                    fr[2], fr[3] = now, fr[3] + len(data)
            for fid in [k for k, fr in self.frames.items()
                        if len(fr[0]) == fr[4] or now - fr[2] > 0.5]:
                self._finish(fid)                 # 收齐的立即确认；没收齐的安静 0.5 秒后记为不完整

    def stop(self):
        self.closed.set()
        if self.ident is not None:
            self.join(2.0)
        for fid in sorted(self.frames):
            self._finish(fid)                     # 停止时还没收齐的帧
        self.sock.close()

    def summary(self):
        span = sorted(r[7] for r in self.rows if r[6] == 1)
        return {"frames_seen": self.n_frames, "complete": self.n_ok,
                "complete_rate": round(self.n_ok / max(1, self.n_frames), 4), "frag_lost": self.n_lost_frag,
                "span_ms_median": rnd(pct(span, 0.5)), "span_ms_p95": rnd(pct(span, 0.95))}


# ---------------- 控制通道 ----------------
class Board:
    """向板端发 JSON 指令，收回复和链路状态流。

    套接字是"已连接"的，从临时端口发出；回流的数据包属于同一段会话，笔记本防火墙不用为它开入站规则。
    """

    def __init__(self, host, port, timeout=1.5):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.connect((host, port))
        self.sock.setblocking(False)
        self.port, self.timeout, self.msg_id = port, timeout, 0
        self.replies, self.link_rows = [], []
        self.lock = threading.Lock()

    @property
    def stream_port(self):
        return self.sock.getsockname()[1]

    def _pump(self):
        while True:
            try:
                data = self.sock.recv(65535)
            except OSError:
                return
            try:
                msg = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(msg, dict):
                continue
            with self.lock:
                if "id" in msg:
                    self.replies.append(msg)
                elif "t" in msg and "l" in msg:
                    self.link_rows.append([int(time.time() * 1000), msg["t"]] + list(msg["l"]))

    def call(self, cmd, tries=3, **kw):
        for _ in range(tries):
            self.msg_id += 1
            rid = self.msg_id
            try:
                self.sock.send(json.dumps(dict(kw, cmd=cmd, id=rid)).encode("utf-8"))
            except OSError:
                time.sleep(0.2)
                continue
            deadline = time.perf_counter() + self.timeout
            while time.perf_counter() < deadline:
                self._pump()
                with self.lock:
                    hit = [m for m in self.replies if m.get("id") == rid]
                    if hit:
                        self.replies.remove(hit[0])
                        return hit[0]
                time.sleep(0.01)
        return None

    def stream_loop(self, stop):
        while not stop.is_set():
            self._pump()
            time.sleep(0.02)


# ---------------- 采集一条 ----------------
def raise_timer_resolution():
    """Windows 上系统定时器默认约 15.6 ms 一跳，旧版 Python 的 time.sleep() 会因此多睡十几毫秒，
    探测包的发送节拍就不稳。把本进程的定时器精度提到 1 ms，程序退出时自动恢复。"""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        winmm = ctypes.WinDLL("winmm")
        if winmm.timeBeginPeriod(1) == 0:
            atexit.register(winmm.timeEndPeriod, 1)
    except Exception:
        pass


def safe_label(text):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(text)).strip("_") or "unlabeled"


def countdown(n):
    n = int(n)
    if n <= 0:
        return
    for left in range(n, 0, -1):
        print("  %d 秒后开始……（Ctrl+C 取消）" % left, flush=True)
        time.sleep(1)
    print("  开始！", flush=True)


def progress_line(rows, sink, elapsed):
    if not rows:
        return "  [%4.0fs] 等待第一个探测包……" % elapsed
    s = summarize(rows)
    line = "  [%4.0fs] 探测 收 %d/%d 丢 %.1f%% ｜ RTT 中位 %s ms ｜ RSSI %s" % (
        elapsed, s["received"], s["sent"], 100 * s["loss_rate"], s["rtt_ms"]["median"],
        "空" if s["rssi_dbm"]["median"] is None else "%s dBm" % s["rssi_dbm"]["median"])
    if sink is not None:
        line += " ｜ 帧 完整 %d/%d" % (sink.n_ok, sink.n_frames)
    return line


def clock_offset(board):
    """返回“板子时钟 − 笔记本时钟”（秒），读不到时返回 None。用一次 hello 往返估计，误差不超过往返时间的一半。"""
    t0 = time.time()
    hello = board.call("hello")
    t1 = time.time()
    if not hello or not hello.get("ok") or "time_ms" not in (hello.get("node") or {}):
        return None
    return hello["node"]["time_ms"] / 1000.0 - (t0 + t1) / 2.0


def sync_clock(board, args):
    """板子一般没有电池时钟，上电后的时间不可信。和笔记本差 1 秒以上，就把板子时钟设成笔记本的时间。

    返回同步之后的偏差（秒）。不改动板子时钟时返回 None。
    """
    if args.no_time_sync:
        return None
    offset = clock_offset(board)
    if offset is None or abs(offset) <= 1.0:
        return None
    r = board.call("settime", epoch_ms=int(time.time() * 1000))
    if not r or not r.get("ok"):
        print("  注意：板子时钟与笔记本相差 %.0f 秒，没能同步（%s）。板端自己的日志会和笔记本的对不上。"
              % (offset, (r or {}).get("error") or "板端没有应答"))
        return None
    now = clock_offset(board)
    print("  板子时钟与笔记本相差 %.0f 秒，已同步为笔记本的时间（现在相差 %.3f 秒）。"
          % (offset, now if now is not None else 0.0))
    return now if now is not None else 0.0


def run_trace(host, spec, args):
    label = safe_label(spec.get("label", "unlabeled"))
    duration = float(spec.get("duration_s", 60))
    kb, fps = parse_load(spec.get("load", ""))
    board = Board(host, args.ctrl_port)
    hello = None if args.no_ctrl else board.call("hello")
    node = hello.get("node") if hello and hello.get("ok") else None
    clock_offset_s = None
    if node:
        board.call("reset")
        clock_offset_s = sync_clock(board, args)
    else:
        print("  注意：控制通道（UDP %d）不通。仍能采集探测数据，但没有链路状态，也不能发帧。" % args.ctrl_port)
        if not args.no_ctrl:
            print("        可以先运行 doctor 查原因。")
        if kb:
            raise SystemExit("带载采集需要板端运行 tools/board_node.py，已停止。")

    started = datetime.now()
    ts = started.strftime("%Y%m%d_%H%M%S")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    probe = Probe(host, args.probe_port, args.hz, args.timeout, args.late_timeout)
    try:
        sink = Sink(args.sink_bind, args.sink_port) if kb else None
    except OSError as exc:
        raise SystemExit("笔记本无法监听 UDP %d（%s）。换一个 --sink-port，或关掉占用端口的程序。" % (args.sink_port, exc))
    stop_stream = threading.Event()
    streamer = threading.Thread(target=board.stream_loop, args=(stop_stream,), daemon=True)
    if node:
        streamer.start()
        board.call("stream", on=True, port=board.stream_port, period_ms=args.stream_ms)
    if sink:
        sink.start()
        board.call("frames", on=True, kb=kb, fps=fps, seconds=0, port=args.sink_port)

    interrupted = False
    try:
        countdown(spec.get("countdown", args.countdown))
        probe.start()
        t0, t_print = time.perf_counter(), time.perf_counter()
        while time.perf_counter() - t0 < duration:
            time.sleep(min(0.5, max(0.05, duration - (time.perf_counter() - t0))))
            if args.progress > 0 and time.perf_counter() - t_print >= args.progress:
                t_print = time.perf_counter()
                print(progress_line(probe.rows(), sink, time.perf_counter() - t0), flush=True)
    except KeyboardInterrupt:
        interrupted = True
        print("\n  已中断。")
    node_frames = None
    if sink:
        node_frames = (board.call("frames", on=False) or {}).get("frames")   # 先停帧：带载区间和探测区间对齐
    probe.stop()
    if sink:
        time.sleep(0.8)                        # 等没收齐的帧被记为不完整
        sink.stop()
    if node:
        board.call("stream", on=False)
    stop_stream.set()
    if streamer.ident is not None:
        streamer.join(1.0)

    rows = probe.rows()
    if not rows:                                # 倒计时阶段就按了 Ctrl+C
        print("  没有开始采集（已取消），不写文件。")
        return None
    summary = summarize(rows)
    counts = {"laptop_sent": len(rows), "laptop_send_fail": probe.n_send_fail,
              "laptop_replies_received": probe.n_replies, "laptop_unmatched": probe.n_unmatched,
              "laptop_max_stall_ms": round(max(0.0, probe.max_gap - 0.05) * 1000.0),   # 空闲时每 0.05 秒醒一次，超出部分是被卡住的时间
              "board": {k: v for k, v in (board.call("status") or {}).items() if k.startswith("probe_")} if node else None}
    csv_path = out / ("probe_%s_%s.csv" % (label, ts))
    json_path = out / ("probe_%s_%s.json" % (label, ts))
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(PROBE_COLS)
        w.writerows(rows)
    if sink:
        with (out / ("frames_sink_%s_%s.csv" % (label, ts))).open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(FRAME_COLS)
            w.writerows(sink.rows)
    if node and board.link_rows:
        with (out / ("boardlink_%s_%s.csv" % (label, ts))).open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["pc_wall_ms", "board_wall_ms"] + STATE_KEYS + ["ap_id"])
            w.writerows(board.link_rows)

    meta = {k: spec.get(k) for k in ("distance_m", "obstacle", "channel", "device", "hotspot", "note",
                                     "inject", "route", "direction", "speed_mps")}
    meta.setdefault("obstacle", "none")
    meta.setdefault("device", "board")
    meta.setdefault("hotspot", "laptop")
    load = None
    if sink:
        load = {"kb": kb, "fps": fps, "mbps": round(kb * 1024 * fps * 8 / 1e6, 2),
                "laptop": sink.summary(), "board": node_frames}
    rtt_med = summary["rtt_ms"]["median"]
    payload = {
        "host": host, "label": label, "hz": args.hz, "duration_s": duration,
        "started_at": started.isoformat(timespec="seconds"),
        "dataset": meta, "summary": summary, "load": load, "node": node, "interrupted": interrupted,
        "counts": counts, "clock_offset_s": rnd(clock_offset_s, 3),
        "rtt_median": rtt_med, "rtt_p95": summary["rtt_ms"]["p95"],
        "rssi_median": summary["rssi_dbm"]["median"], "jitter_median": summary["rtt_ms"]["jitter_median"],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    row = {
        "started_at": payload["started_at"], "label": label, "csv": csv_path.name, "json": json_path.name,
        "duration_s": duration,
        **{k: meta.get(k) for k in ("distance_m", "obstacle", "channel", "hotspot", "note")},
        "inject": json.dumps(meta["inject"], ensure_ascii=False) if meta["inject"] else "",
        "sent": summary["sent"], "received": summary["received"], "loss_rate": summary["loss_rate"],
        "rtt_median": rtt_med, "rtt_p95": summary["rtt_ms"]["p95"], "rssi_median": summary["rssi_dbm"]["median"],
    }
    manifest = out / "manifest.csv"
    with manifest.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_COLS)
        if manifest.stat().st_size == 0:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in MANIFEST_COLS})

    print("  完成 %s：%d 秒，探测 收 %d/%d（丢 %.1f%%，迟到回包 %d）｜ RTT 中位 %s ms ｜ P95 %s ms ｜ RSSI %s" % (
        label, duration, summary["received"], summary["sent"], 100 * summary["loss_rate"],
        summary["late_replies"], rtt_med, summary["rtt_ms"]["p95"],
        "空" if summary["rssi_dbm"]["median"] is None else "%s dBm" % summary["rssi_dbm"]["median"]))
    if summary["dl_lost_total"]:
        print("        其中去程（笔记本→板子）丢的探测包：%d 个" % summary["dl_lost_total"])
    g = summary["send_gap_ms"]
    if g["off_cadence_share"] > 0.10:           # 系统偶尔卡一下很正常；多了说明笔记本太忙或定时器不准
        print("        注意：有 %.0f%% 的探测包偏离发送节拍 5 ms 以上。"
              "采集时不要在笔记本上同时跑占 CPU 的程序。" % (100 * g["off_cadence_share"]))
    if counts["board"]:
        b = counts["board"]
        print("        计数：笔记本发出 %d（发送失败 %d），收到回包 %d（对不上序号 %d）｜ "
              "板端收到请求 %s，发出回包 %s（发送失败 %s）｜ 处理循环最长停顿：笔记本 %d ms，板端 %s ms" % (
                  counts["laptop_sent"], counts["laptop_send_fail"], counts["laptop_replies_received"],
                  counts["laptop_unmatched"], b.get("probe_req"), b.get("probe_rsp"), b.get("probe_send_fail"),
                  counts["laptop_max_stall_ms"], b.get("probe_max_stall_ms")))
    if load:
        ls = load["laptop"]
        print("        负载：%s，笔记本收到 %d 帧，完整 %d（%.1f%%）" % (
            load_text(kb, fps), ls["frames_seen"], ls["complete"], 100 * ls["complete_rate"]))
    print("        已保存：%s" % csv_path)
    return row


# ---------------- 按计划采集 ----------------
def expand_plan(path, only, limit):
    runs = json.loads(Path(path).read_text(encoding="utf-8-sig")).get("runs", [])
    if only:
        keep = {s.strip() for s in only.split(",") if s.strip()}
        runs = [r for r in runs if r.get("label") in keep]
        if not runs:
            raise SystemExit("--only 里没有任何计划中的 label：%s" % keep)
    specs = []
    for run in runs:
        for k in range(1, int(run.get("repeat", 1)) + 1):   # repeat：同一条重复采几遍
            spec = dict(run)
            if int(run.get("repeat", 1)) > 1:
                spec["label"] = "%s_%d" % (run["label"], k)
            specs.append(spec)
    return specs[:limit] if limit else specs


def run_plan(args):
    specs = expand_plan(args.plan, args.only, args.limit)
    total = sum(float(s.get("duration_s", 60)) for s in specs) / 60.0
    print("计划：%s ｜ %d 条 ｜ 总时长约 %.0f 分钟 ｜ 板端 %s" % (args.plan, len(specs), total, args.board))
    print("输出目录：%s" % Path(args.out).resolve())
    if args.dry_run:
        for i, s in enumerate(specs, 1):
            print("  %2d. %-24s %4s 秒  负载 %-8s %s" % (i, s["label"], s.get("duration_s", 60),
                                                          s.get("load", "无"), s.get("note", "")))
        print("\n（--dry-run：没有开始采集）")
        return 0
    rows = []
    try:
        for i, spec in enumerate(specs, 1):
            print("\n\n== 第 %d/%d 条 ==" % (i, len(specs)))
            print("[%s] 时长 %s 秒 ｜ 负载 %s ｜ 路线 %s ｜ 方向 %s ｜ 速度 %s m/s" % (
                spec["label"], spec.get("duration_s", 60), spec.get("load", "无"),
                spec.get("route", "-"), spec.get("direction", "-"), spec.get("speed_mps", "-")))
            if spec.get("note"):
                print("  现场动作：%s" % spec["note"])
            if not args.yes:
                input("  摆好位置后按 Enter 开始（Ctrl+C 中止）…… ")
            row = run_trace(args.board, spec, args)
            if row:
                rows.append(row)
    except KeyboardInterrupt:
        print("\n已中止（已完成的条目都已落盘并写入 manifest.csv）")
    print("\n采集结束：%d 条已记录 → %s" % (len(rows), Path(args.out).resolve() / "manifest.csv"))
    print('下一步：python ml/build_dataset.py --traces "%s/probe_*.csv"' % args.out)
    return 0


# ---------------- 自检 ----------------
def ping(host, n=4):
    windows = sys.platform.startswith("win")
    cmd = ["ping", "-n" if windows else "-c", str(n)] + ([] if windows else ["-W", "2"]) + [host]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=25)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    try:
        text = r.stdout.decode("utf-8")
    except UnicodeDecodeError:
        text = r.stdout.decode("gbk", errors="replace")
    return r.returncode == 0, text


def doctor(args):
    host = args.board
    print("目标 %s ｜ 探测 UDP %d ｜ 控制 UDP %d ｜ 帧发到笔记本的 UDP %d（监听 %s）\n"
          % (host, args.probe_port, args.ctrl_port, args.sink_port, args.sink_bind))
    hints = []

    print("[1/4] ICMP ping")
    ok, text = ping(host)
    print("   " + "\n   ".join([ln.strip() for ln in text.splitlines() if ln.strip()][-4:]))
    if not ok:
        hints.append("ping 不通：板子没连上同一个热点，或者 IP 不对。在板子上运行 ip addr 看地址，"
                     "在笔记本上运行 arp -a 看板子在不在线。")

    print("\n[2/4] 探测回显（UDP %d，2 秒）" % args.probe_port)
    probe = Probe(host, args.probe_port, 20, args.timeout, args.late_timeout)
    probe.start()
    time.sleep(2.0)
    probe.stop()
    s = summarize(probe.rows())
    print("   发 %d 收 %d ｜ RTT 中位 %s ms ｜ RSSI %s" % (
        s["sent"], s["received"], s["rtt_ms"]["median"],
        "空" if s["rssi_dbm"]["median"] is None else "%s dBm" % s["rssi_dbm"]["median"]))
    if s["received"] == 0:
        hints.append("ping 通但回显全丢：板端节点没有启动、监听地址或端口不对。"
                     "在板子上运行 python3 board_node.py，并确认它打印的端口是 UDP %d。" % args.probe_port)

    print("\n[3/4] 控制通道（UDP %d）" % args.ctrl_port)
    board = Board(host, args.ctrl_port)
    hello = board.call("hello")
    node = hello.get("node") if hello and hello.get("ok") else None
    if node:
        print("   板端：%s ｜ %s ｜ Python %s" % (node["hostname"], node["system"], node["python"]))
        print("   RSSI：%s ｜ 内核收包时间戳：%s" % (
            "有（%s）" % node["link_source"] if node["has_rssi"] else "没有（%s）" % node["link_source"],
            "开" if node["kernel_ts"] else "关"))
        if not args.no_time_sync:
            sync_clock(board, args)
            off = clock_offset(board)
            if off is not None:
                print("   时钟：板端与笔记本相差 %.3f 秒" % off + ("" if abs(off) <= 1.0 else "（差得太多，板端自己的日志会和笔记本的对不上）"))
    else:
        hints.append("探测通但控制通道不通：板端跑的不是 tools/board_node.py（例如只有回显功能），"
                     "或者 UDP %d 被占用。可以采集探测数据，但没有链路状态，也不能发帧。" % args.ctrl_port)

    # 用平稳的小包模拟视频码流（每个包 1200 字节，和 RTP 包差不多大），而不是几十 KB 的突发：
    # 突发会让驱动队列溢出，平稳发送才是视频流的真实形态。
    seconds, rate_mbps = 3.0, 3.0
    print("\n[4/4] 模拟视频码流（约 %g Mbps，持续 %g 秒）" % (rate_mbps, seconds))
    if node:
        try:
            sink = Sink(args.sink_bind, args.sink_port)
            sink.start()
            board.call("frames", on=True, kb=FRAG_SIZE / 1024.0, fps=rate_mbps * 1e6 / 8.0 / FRAG_SIZE,
                       seconds=seconds, port=args.sink_port)
            time.sleep(seconds + 1.6)
            node_frames = (board.call("frames", on=False) or {}).get("frames") or {}
            sink.stop()
            ls = sink.summary()
            sent = node_frames.get("frames_sent")
            goodput = ls["complete"] * FRAG_SIZE * 8 / seconds / 1e6
            print("   板端发出 %s 个包 ｜ 笔记本收到 %d 个，完整 %d ｜ 实际送达约 %.1f Mbps" % (
                sent if sent is not None else "?", ls["frames_seen"], ls["complete"], goodput))
            expected = rate_mbps * 1e6 / 8.0 / FRAG_SIZE * seconds
            if ls["frames_seen"] == 0:
                if args.sink_bind in ("127.0.0.1", "localhost"):
                    why = "笔记本只在回环地址上收包（--sink-bind %s），板子的包到不了" % args.sink_bind
                else:
                    why = "笔记本防火墙很可能拦了入站 UDP %d" % args.sink_port
                hints.append("板端发了包但笔记本一个没收到：%s。防火墙的问题：在 Windows 防火墙的入站规则里"
                             "允许 Python（专用和公用都勾）；地址的问题：去掉 --sink-bind，或改成笔记本网卡的地址。" % why)
            elif sent and sent < 0.7 * expected:
                hints.append("板端在 %g 秒内只发出 %d 个包（应有约 %d 个）：板子的发送被阻塞了。"
                             "先确认板子离热点近、信号够强，再开始采集。" % (seconds, sent, int(expected)))
            elif ls["complete_rate"] < 0.99:
                hints.append("近距离就有 %.0f%% 的包丢失，先确认两台设备挨得很近、天线接好、热点信道不拥挤，再开始采集。"
                             % (100 * (1 - ls["complete_rate"])))
        except OSError as exc:
            hints.append("笔记本无法监听 UDP %d：%s。换一个 --sink-port。" % (args.sink_port, exc))
    else:
        print("   跳过（控制通道不通）")

    print("\n==== 结论 ====")
    if not hints:
        print("链路已跑通：探测、控制、收帧都正常。可以开始采集。")
    else:
        for i, h in enumerate(hints, 1):
            print("%d. %s" % (i, h))
    return 0 if not hints else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="笔记本侧采集器：探测、收帧、收链路状态并落盘")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--board", required=True, help="板端节点的 IP")
        p.add_argument("--probe-port", type=int, default=9000)
        p.add_argument("--ctrl-port", type=int, default=9002)
        p.add_argument("--sink-port", type=int, default=9001, help="笔记本收帧的端口")
        p.add_argument("--sink-bind", default="0.0.0.0", help="笔记本收帧的监听地址")
        p.add_argument("--out", default="traces", help="输出目录")
        p.add_argument("--timeout", type=float, default=0.2, help="回包超过这么多秒没到就记为丢包")
        p.add_argument("--late-timeout", type=float, default=1.0, help="迟到的回包最多等多久")
        p.add_argument("--no-ctrl", action="store_true", help="板端只有回显功能（如 ESP32 固件）时使用")
        p.add_argument("--no-time-sync", action="store_true",
                       help="不把板子的时钟同步成笔记本的时间（默认在相差 1 秒以上时同步）")
        p.add_argument("--no-probe-qos", action="store_true",
                       help="不给笔记本发出的探测包打低时延标记（默认打上）")

    r = sub.add_parser("run", help="采集一条 trace")
    common(r)
    r.add_argument("--label", required=True)
    r.add_argument("--duration", type=float, default=60, help="采集多少秒")
    r.add_argument("--load", default="", help="带载：每帧KBx每秒帧数，例如 50x10；不填则只做探测")
    r.add_argument("--route", default="")
    r.add_argument("--direction", default="", help="fwd / back")
    r.add_argument("--speed", type=float, default=None, help="行走速度（m/s）")
    r.add_argument("--distance-m", type=float, default=None)
    r.add_argument("--obstacle", default="none")
    r.add_argument("--channel", default=None)
    r.add_argument("--hotspot", default="laptop")
    r.add_argument("--note", default="")
    p = sub.add_parser("plan", help="按计划逐条采集")
    common(p)
    p.add_argument("--plan", default="plans/collect_plan.json")
    p.add_argument("--only", default="", help="只采这些 label，逗号分隔")
    p.add_argument("--limit", type=int, default=0, help="最多采几条（0 = 全部）")
    p.add_argument("--yes", action="store_true", help="不等待回车")
    p.add_argument("--dry-run", action="store_true", help="只打印计划")
    d = sub.add_parser("doctor", help="自检：检查板子和笔记本之间的链路")
    common(d)
    for q in (r, p):
        q.add_argument("--hz", type=float, default=20.0, help="探测频率")
        q.add_argument("--stream-ms", type=int, default=200, help="板端上报链路状态的间隔（毫秒）")
        q.add_argument("--progress", type=float, default=5.0, help="实时打印的间隔（秒），0 = 关闭")
        q.add_argument("--countdown", type=int, default=0, help="开始前倒数多少秒")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # 报错信息也走 UTF-8，否则和正常输出混在一起会乱码
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    raise_timer_resolution()
    Probe.mark_probes = not getattr(args, "no_probe_qos", False)

    if args.cmd == "run":
        spec = {"label": args.label, "duration_s": args.duration, "load": args.load, "countdown": args.countdown,
                "route": args.route, "direction": args.direction, "speed_mps": args.speed,
                "distance_m": args.distance_m, "obstacle": args.obstacle, "channel": args.channel,
                "hotspot": args.hotspot, "note": args.note}
        run_trace(args.board, spec, args)
        return 0
    if args.cmd == "plan":
        return run_plan(args)
    return doctor(args)


if __name__ == "__main__":
    raise SystemExit(main())
