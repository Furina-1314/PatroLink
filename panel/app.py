#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PatroLink 可视化面板（Streamlit）。

三个页签，对应"过程 → 成果 → 数据"：
    1) 采集回放：一条 trace 的 RTT/RSSI/丢包曲线 + 恶化区间 + 模型预测概率 + 提前量
    2) 模型评估：AUC/F1/误报/提前量对比（含基线）、ROC/PR、预测-真值时间序列
    3) 数据总览：manifest 记账表、场景覆盖、缺失项提醒
    4) 使用说明：演示时怎么点、怎么说

运行：
    pip install -r requirements.txt        # 含 streamlit / plotly
    streamlit run panel/app.py
    浏览器打开 http://localhost:8501
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ml"))
from build_dataset import FEATURES, compute_rows, load_trace  # noqa: E402

NAVY = "#2F5597"
ORANGE = "#C55A11"
TEAL = "#1F7A6C"
GRAY = "#8C8C8C"
DEGRADE_FILL = "rgba(197,90,17,0.12)"

st.set_page_config(page_title="PatroLink 可视化面板", page_icon="📡", layout="wide")


# ---------------------------------------------------------------- 数据访问
@st.cache_data(show_spinner=False)
def list_traces(traces_dir: str):
    d = Path(traces_dir)
    if not d.is_dir():
        return []
    files = sorted(p for p in d.glob("*.csv") if p.name != "manifest.csv")
    return [str(p) for p in files]


@st.cache_data(show_spinner=False)
def load_trace_df(path_str: str) -> pd.DataFrame:
    df = pd.read_csv(path_str, encoding="utf-8-sig")
    for col in ("rtt_ms", "rssi_dbm"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["lost"] = pd.to_numeric(df["lost"], errors="coerce").fillna(1).astype(int)
    df = df.sort_values("wall_ms").reset_index(drop=True)
    df["t_s"] = (df["wall_ms"] - df.loc[0, "wall_ms"]) / 1000.0
    return df


@st.cache_data(show_spinner=False)
def load_meta(path_str: str) -> dict:
    p = Path(path_str).with_suffix(".json")
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


@st.cache_data(show_spinner=False)
def manifest_df(traces_dir: str) -> pd.DataFrame:
    p = Path(traces_dir) / "manifest.csv"
    if not p.is_file():
        return pd.DataFrame()
    df = pd.read_csv(p, encoding="utf-8-sig")
    return df


def artifact_dirs(artifacts_root: Path):
    out = []
    if artifacts_root.is_dir():
        for d in sorted(artifacts_root.rglob("model.joblib")):
            out.append(d.parent)
    return out


@st.cache_resource(show_spinner=False)
def load_bundle(model_path: str) -> dict:
    import joblib
    return joblib.load(model_path)


@st.cache_data(show_spinner=False)
def metrics_of(dir_str: str) -> dict:
    p = Path(dir_str) / "metrics.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ---------------------------------------------------------------- 计算
def trace_arrays(df: pd.DataFrame):
    return {
        "name": "panel",
        "t": df["t_s"].to_numpy(dtype=float),
        "rtt": df["rtt_ms"].to_numpy(dtype=float),
        "rssi": df["rssi_dbm"].to_numpy(dtype=float),
        "lost": df["lost"].to_numpy(dtype=int),
    }


def predict_trace(df: pd.DataFrame, bundle: dict):
    arr = trace_arrays(df)
    w = float(bundle.get("window_s", 2.0))
    h = float(bundle.get("horizon_s", 1.0))
    s = float(bundle.get("step_s", 0.2))
    rows = compute_rows(arr, w, h, s,
                        float(bundle.get("rtt_thresh", 80.0)),
                        float(bundle.get("loss_thresh", 0.03)))
    if not rows:
        return None
    X = np.array([[r[f] for f in FEATURES] for r in rows], dtype=float)
    med = np.asarray(bundle.get("impute_median", np.zeros(len(FEATURES))))
    X = np.where(np.isnan(X), med, X)
    p = bundle["model"].predict_proba(X)[:, 1]
    return {
        "t": np.array([r["t_s"] for r in rows]),
        "p": p,
        "y": np.array([r["label"] for r in rows], dtype=int),
        "thr": float(bundle.get("threshold", 0.5)),
        "rtt_thresh": float(bundle.get("rtt_thresh", 80.0)),
        "loss_thresh": float(bundle.get("loss_thresh", 0.03)),
        "w": w, "h": h, "s": s,
    }


def degradation_segments(t: np.ndarray, y: np.ndarray):
    """把连续 label=1 的窗口合并成区间，用于画背景色块。"""
    segs, start = [], None
    for i, v in enumerate(y):
        if v == 1 and start is None:
            start = t[i]
        elif v == 0 and start is not None:
            segs.append((start, t[i]))
            start = None
    if start is not None:
        segs.append((start, t[-1] + 0.2))
    return segs


def lead_times(pred: dict, lookback: float = 5.0):
    """每个恶化事件前，最早被预测到的时间差（秒）。"""
    t, p, y, thr = pred["t"], pred["p"], pred["y"], pred["thr"]
    leads, prev = [], 0
    for i in range(len(y)):
        if y[i] == 1 and prev == 0:
            onset = t[i]
            first = None
            j = i - 1
            while j >= 0 and t[j] >= onset - lookback:
                if p[j] >= thr:
                    first = t[j]
                j -= 1
            if first is not None:
                leads.append(onset - first)
        prev = y[i]
    return leads


def rolling_mean(x: np.ndarray, k: int) -> np.ndarray:
    if k <= 1:
        return x
    out = np.full_like(x, np.nan, dtype=float)
    for i in range(len(x)):
        lo = max(0, i - k + 1)
        v = x[lo:i + 1]
        v = v[~np.isnan(v)]
        if v.size:
            out[i] = float(np.mean(v))
    return out


# ---------------------------------------------------------------- 图
def actual_segments(t, rtt_smooth, loss_smooth, rtt_th, loss_th):
    """按"真实指标"标出恶化区间：RTT 平滑值超阈值 或 丢包率超阈值。"""
    bad = []
    for i in range(len(t)):
        r = rtt_smooth[i] if not np.isnan(rtt_smooth[i]) else 0.0
        l = loss_smooth[i] if not np.isnan(loss_smooth[i]) else 0.0
        bad.append(1 if (r >= rtt_th or l >= loss_th) else 0)
    segs, start_i = [], None
    for i, v in enumerate(bad):
        if v == 1 and start_i is None:
            start_i = i
        elif v == 0 and start_i is not None:
            segs.append((float(t[start_i]), float(t[i])))
            start_i = None
    if start_i is not None:
        segs.append((float(t[start_i]), float(t[-1])))
    return segs


def lead_times(df: pd.DataFrame, pred: dict, lookback: float = 5.0):
    """真实恶化区间起点 之前，最早被预测到的时间差（秒）。"""
    t_raw = df["t_s"].to_numpy(dtype=float)
    rtt = df["rtt_ms"].to_numpy(dtype=float)
    lost = df["lost"].to_numpy(dtype=int)
    dt = float(np.nanmedian(np.diff(t_raw))) if len(t_raw) > 2 else 0.05
    k = max(1, int(round(2.0 / dt)))
    rtt_s = rolling_mean(rtt, k)
    loss_s = rolling_mean(lost.astype(float), k)
    rtt_th = float(pred.get("rtt_thresh", 80.0))
    loss_th = float(pred.get("loss_thresh", 0.03))
    segs = actual_segments(t_raw, rtt_s, loss_s, rtt_th, loss_th)

    leads = []
    for seg_start, _ in segs:
        before = np.where(pred["t"] < seg_start)[0]
        first = None
        for j in before[::-1]:
            if pred["t"][j] < seg_start - lookback:
                break
            if pred["p"][j] >= pred["thr"]:
                first = float(pred["t"][j])
        if first is not None:
            leads.append(seg_start - first)
    return leads


def fig_replay(df: pd.DataFrame, pred: dict | None, window: tuple[float, float]):
    t = df["t_s"].to_numpy()
    rtt = df["rtt_ms"].to_numpy(dtype=float)
    rssi = df["rssi_dbm"].to_numpy(dtype=float)
    lost = df["lost"].to_numpy(dtype=int)
    dt = float(np.nanmedian(np.diff(t))) if len(t) > 2 else 0.05
    k = max(1, int(round(2.0 / dt)))
    rtt_smooth = rolling_mean(rtt, k)
    loss_smooth = rolling_mean(lost.astype(float), k)

    rtt_th = float(pred.get("rtt_thresh", 80.0)) if pred else 80.0
    loss_th = float(pred.get("loss_thresh", 0.03)) if pred else 0.03
    segs = actual_segments(t, rtt_smooth, loss_smooth, rtt_th, loss_th)

    fig = make_subplots(rows=3, cols=1, shared_xaxes=True,
                        row_heights=[0.52, 0.28, 0.20], vertical_spacing=0.06,
                        specs=[[{"secondary_y": True}], [{}], [{}]],
                        subplot_titles=("RTT 与预测概率", "RSSI", "丢包率（2s 平滑）"))
    for x0, x1 in segs:
        fig.add_vrect(x0=x0, x1=x1, fillcolor=DEGRADE_FILL, line_width=0,
                      layer="below", row=1, col=1)
        fig.add_vrect(x0=x0, x1=x1, fillcolor=DEGRADE_FILL, line_width=0,
                      layer="below", row=2, col=1)

    fig.add_trace(go.Scatter(x=t, y=rtt, mode="markers", name="RTT 采样",
                             marker=dict(size=3, color=NAVY, opacity=0.30)),
                  row=1, col=1)
    fig.add_trace(go.Scatter(x=t, y=rtt_smooth, mode="lines", name="RTT 2s 均值",
                             line=dict(color=NAVY, width=2)), row=1, col=1)
    fig.add_trace(go.Scatter(x=t, y=np.full_like(t, rtt_th), mode="lines",
                             name=f"RTT 阈值 {rtt_th:g} ms",
                             line=dict(color=GRAY, dash="dash", width=1)),
                  row=1, col=1)

    if pred is not None:
        fig.add_trace(go.Scatter(x=pred["t"], y=pred["p"], mode="lines",
                                 name="预测 P(恶化)",
                                 line=dict(color=ORANGE, width=2)),
                      row=1, col=1, secondary_y=True)
        fig.add_hline(y=pred["thr"], line=dict(color=ORANGE, dash="dot", width=1),
                      row=1, col=1, secondary_y=True,
                      annotation_text=f"判定阈值 {pred['thr']:.2f}",
                      annotation_position="bottom left")
    fig.update_yaxes(title_text="RTT (ms)", row=1, col=1, secondary_y=False)
    fig.update_yaxes(title_text="P(恶化)", row=1, col=1, secondary_y=True,
                     range=[-0.03, 1.05], showgrid=False)

    fig.add_trace(go.Scatter(x=t, y=rssi, mode="lines", name="RSSI",
                             line=dict(color=TEAL, width=1.4)), row=2, col=1)
    fig.update_yaxes(title_text="dBm", row=2, col=1)

    fig.add_trace(go.Scatter(x=t, y=loss_smooth * 100, mode="lines",
                             name="丢包率(%)", fill="tozeroy",
                             line=dict(color=ORANGE, width=1.2)), row=3, col=1)
    fig.update_yaxes(title_text="%", row=3, col=1)
    fig.update_xaxes(title_text="时间 (s)", row=3, col=1)
    fig.update_layout(height=680, margin=dict(l=10, r=10, t=40, b=10),
                      legend=dict(orientation="h", yanchor="bottom", y=1.02),
                      hovermode="x unified")
    fig.update_xaxes(range=list(window))
    return fig


def fig_metric_bars(metrics: dict):
    models = metrics.get("models", {})
    if not models:
        return None
    names = list(models)
    fig = go.Figure()
    for key, label, color in (("auc_mean", "AUC", NAVY), ("f1_mean", "F1", ORANGE),
                              ("recall_mean", "Recall", TEAL)):
        fig.add_trace(go.Bar(name=label, x=names,
                             y=[models[m].get(key, 0) for m in names],
                             marker_color=color))
    fig.update_layout(barmode="group", height=320, margin=dict(l=10, r=10, t=30, b=10),
                      legend=dict(orientation="h", yanchor="bottom", y=1.02),
                      yaxis_range=[0, 1])
    return fig


# ---------------------------------------------------------------- UI
st.title("📡 PatroLink · 链路数据与预测面板")
st.caption("采集回放 → 模型评估 → 数据总览。所有数字都来自本机 `traces/` 与 `ml/artifacts/`，不手工补数。")

with st.sidebar:
    st.header("数据源")
    traces_dir = st.text_input("traces 目录", str(ROOT / "traces"))
    artifacts_root = st.text_input("模型产物目录", str(ROOT / "ml" / "artifacts"))
    if st.button("🔄 刷新缓存"):
        st.cache_data.clear()
        st.cache_resource.clear()
        st.rerun()

    trace_files = list_traces(traces_dir)
    using_demo = False
    if not trace_files:
        demo = list_traces(str(ROOT / "traces" / "demo"))
        if demo:
            trace_files = demo
            using_demo = True
    st.metric("已采 trace", len(trace_files))
    model_dirs = artifact_dirs(Path(artifacts_root))
    st.metric("已训练模型", len(model_dirs))

    model_dir = None
    if model_dirs:
        labels = [str(d.relative_to(ROOT)) for d in model_dirs]
        idx = st.selectbox("选择模型", range(len(labels)),
                           format_func=lambda i: labels[i])
        model_dir = str(model_dirs[idx])
    st.markdown("—")
    st.caption("提示：先把探针 CSV 放进 traces/，再跑 `ml/train_predictor.py`；两侧都有数据时面板最完整。")

tabs = st.tabs(["🎬 采集回放", "📈 模型评估", "🗂 数据总览", "❓ 使用说明"])

# ---------------- Tab 1
with tabs[0]:
    if using_demo:
        st.warning("当前显示的是**合成演示数据**（traces/demo）——只用于预览面板；"
                   "真实数据放进 traces/ 后会自动切换。")
    if not trace_files:
        st.info("还没有采集数据。先按 `docs/collect_playbook.md` 跑：\n\n"
                "`python pc/collect_dataset.py --host <ESP32-IP>`")
    else:
        names = [Path(f).stem for f in trace_files]
        sel = st.selectbox("选择一条 trace", range(len(names)),
                           format_func=lambda i: names[i])
        path = trace_files[sel]
        df = load_trace_df(path)
        meta = load_meta(path).get("dataset", {})

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("时长", f"{df['t_s'].iloc[-1]:.0f} s")
        c2.metric("收包率", f"{1 - df['lost'].mean():.1%}")
        c3.metric("RTT 中位", f"{np.nanmedian(df['rtt_ms']):.1f} ms")
        c4.metric("RTT P95", f"{np.nanpercentile(df['rtt_ms'], 95):.1f} ms")
        c5.metric("RSSI 中位", f"{np.nanmedian(df['rssi_dbm']):.0f} dBm")
        if meta:
            st.caption("场景：距离 {} m ｜ 遮挡 {} ｜ 信道 {} ｜ 热点 {} ｜ {}".format(
                meta.get("distance_m"), meta.get("obstacle"), meta.get("channel"),
                meta.get("hotspot"), meta.get("note", "")))

        dur = float(df["t_s"].iloc[-1])
        lo, hi = st.slider("显示区间 (s)", 0.0, max(dur, 1.0), (0.0, max(dur, 1.0)), 0.5)

        bundle = None
        if model_dir:
            try:
                bundle = load_bundle(str(Path(model_dir) / "model.joblib"))
            except Exception as exc:
                st.warning(f"模型加载失败：{exc}")
        pred = predict_trace(df, bundle) if bundle else None

        fig = fig_replay(df, pred, (lo, hi))
        st.plotly_chart(fig, width="stretch")

        if pred is not None:
            leads = lead_times(df, pred)
            m1, m2, m3 = st.columns(3)
            m1.metric("预测覆盖", f"{len(pred['t'])} 个窗口")
            m2.metric("提前量中位", f"{np.median(leads):.1f} s" if leads else "—")
            m3.metric("判定阈值", f"{pred['thr']:.2f}")
            st.caption("橙色背景 = 真实恶化窗口（由未来 1 s 的 RTT/丢包算出）；橙线 = 模型预测概率，"
                       "线在背景之前抬起就是“提前预警”。")
        else:
            st.caption("橙色背景 = 真实恶化窗口。侧边栏选一个已训练模型，可叠加预测概率曲线。")

        with st.expander("看这张图该说什么（答辩口径）"):
            st.markdown(
                "- **先指背景**：这些橙色区间是“真的变差了”的时间段；\n"
                "- **再指橙线**：模型概率在背景之前抬头，说明它提前看到了；\n"
                "- **最后指 RTT/RSSI**：恶化前 1–2 s，RTT 先抬、RSSI 先掉、丢包最后才来——这就是可用的前兆。")

# ---------------- Tab 2
with tabs[1]:
    if not model_dirs:
        st.info("还没有训练产物。先跑：\n\n"
                "`python ml/build_dataset.py --traces \"traces/probe_*.csv\"`\n\n"
                "`python ml/train_predictor.py --data ml/artifacts/dataset.csv --split trace`")
    else:
        metrics = metrics_of(model_dir)
        if not metrics:
            st.warning("该模型目录下没有 metrics.json")
        else:
            st.subheader("模型 vs 基线（各 fold 平均）")
            rows = []
            for name, v in metrics.get("models", {}).items():
                rows.append({
                    "模型": name,
                    "AUC": v.get("auc_mean"),
                    "F1": v.get("f1_mean"),
                    "Recall": v.get("recall_mean"),
                    "PR-AUC": v.get("pr_auc_mean"),
                })
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

            fig = fig_metric_bars(metrics)
            if fig is not None:
                st.plotly_chart(fig, width="stretch")

            lead = metrics.get("lead_time_s")
            quiet = []
            for f in metrics.get("folds", []):
                if f.get("quiet"):
                    for m in ("rule", "logreg", "hgb"):
                        src = f.get(m) or f["baselines"].get(m)
                        if src:
                            quiet.append({"trace": f["fold"], "模型": m,
                                          "误报/分钟": src.get("alarms_per_min")})
            cc1, cc2 = st.columns([1, 1])
            with cc1:
                st.markdown("**预测提前量**")
                if lead:
                    st.metric("中位数", f"{lead.get('median')} s")
                    st.caption(f"P90 {lead.get('p90')} s ｜ 事件数 {lead.get('n_events')}")
                else:
                    st.caption("该划分下没有统计到提前量")
            with cc2:
                st.markdown("**静默 trace 的误报率**")
                if quiet:
                    st.dataframe(pd.DataFrame(quiet), width="stretch", hide_index=True)
                else:
                    st.caption("本次划分没有静默 trace（建议下次采 2 条全程良好的）")

            imgs = st.columns(2)
            for col, fname, title in ((imgs[0], "roc_pr.png", "ROC / PR 曲线"),
                                      (imgs[1], "timeseries.png", "预测概率 vs 真实恶化")):
                p = Path(model_dir) / fname
                with col:
                    st.markdown(f"**{title}**")
                    if p.is_file():
                        st.image(str(p), width="stretch")
                    else:
                        st.caption("未生成该图（训练时加了 --no-plot？）")

# ---------------- Tab 3
with tabs[2]:
    man = manifest_df(traces_dir)
    if not man.empty:
        st.subheader("采集记账表（manifest.csv）")
        st.dataframe(man, width="stretch", hide_index=True)
        if "obstacle" in man.columns:
            st.markdown("**场景覆盖**")
            cov = man.groupby("obstacle").size().reset_index(name="条数")
            st.dataframe(cov, width="stretch", hide_index=True)
            goods = int((man["obstacle"] == "none").sum()) if "obstacle" in man else 0
            if goods < 2:
                st.warning("全程良好（obstacle=none）的 trace 少于 2 条：模型误报率会高估自己，建议补采。")
    elif trace_files:
        st.info("没有 manifest.csv，直接从 CSV 估算统计：")
        rows = []
        for f in trace_files:
            df = load_trace_df(f)
            rows.append({
                "trace": Path(f).stem,
                "时长(s)": round(float(df["t_s"].iloc[-1]), 1),
                "收包率": round(float(1 - df["lost"].mean()), 4),
                "RTT中位(ms)": round(float(np.nanmedian(df["rtt_ms"])), 1),
                "RTT P95(ms)": round(float(np.nanpercentile(df["rtt_ms"], 95)), 1),
                "RSSI中位(dBm)": round(float(np.nanmedian(df["rssi_dbm"])), 1),
            })
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    else:
        st.info("还没有数据。")

    st.markdown("---")
    st.markdown("**数据规模估算**")
    if trace_files:
        total_s = 0.0
        total_n = 0
        for f in trace_files:
            df = load_trace_df(f)
            total_s += float(df["t_s"].iloc[-1])
            total_n += len(df)
        st.markdown(f"- 共 {len(trace_files)} 条，合计 {total_s/60:.1f} 分钟，{total_n} 个包；"
                    f"约 {total_n/20/0.2:.0f} 个滑窗样本（20 Hz、步长 0.2 s）")

# ---------------- Tab 4
with tabs[3]:
    st.markdown(
        """
### 演示时怎么点（3 分钟版）

1. **侧边栏**确认已采 trace 数、已训练模型数；
2. **采集回放**：选 `wall_5m` → 看到 RTT 台阶 + 橙色恶化区间；
   再选 `good_desk` → 曲线平稳、几乎没有橙色区间（对比出"位置真的影响链路"）；
3. 选一条有预测叠加的 → 指橙色曲线在背景之前抬头 → 说"提前量 X 秒"；
4. **模型评估**：指表格里 `hgb/logreg` 比 `rule` 的 F1/误报更好 → 说"不是拍脑袋调阈值"；
5. **数据总览**：manifest 的 9 行 → 说"每条都有距离/遮挡/注入参数，可复现"。

### 这个面板和最终系统什么关系

- 现在：展示**链路数据 + 预测器**（Step 2a/4）；
- 下一步：接入 `timeline.jsonl` 后，把"动作、奖励、决策解释"画成同样的时间线（Step 5/6）——
  数据源从 `traces/probe_*.csv` 换成 `logs/*.jsonl`，曲线换成"每一帧的耗时分解 + 动作标记"。

### 不做什么

- 不手工补数：所有曲线都从 CSV/JSON 现算；
- 不把面板做成装饰：每张图都对应一个可回答的问题（哪条链路、什么时候恶化、提前多久）。
"""
    )
