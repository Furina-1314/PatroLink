#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""训练链路风险预测器（监督学习），并与两个"不用训练"的基线对比。

预测目标：未来 1 s 内链路是否恶化（RTT P95 ≥ 阈值 或 丢包率 ≥ 阈值）。
基线：
    persistence  当前 RTT P95 已经超阈值 → 判 1（"已经坏了"）
    rule         当前 RTT 中位数 > 0.8×阈值 或 丢包率 > 0.5×阈值 → 判 1
模型：
    logreg       标准化 + 逻辑回归（可解释，系数即权重）
    hgb          梯度提升树（HistGradientBoosting，主力）

划分（两种都要看）：
    --split time   每条 trace 按时间前 70% 训练、后 30% 测试（加 gap 防重叠泄漏）
    --split trace  留一条 trace 不参与训练，用它测试（衡量"换场景还能不能用"）

用法：
    python ml/build_dataset.py --traces traces/demo/*.csv
    python ml/train_predictor.py --data ml/artifacts/dataset.csv --split time
    python ml/train_predictor.py --data ml/artifacts/dataset.csv --split trace
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (average_precision_score, confusion_matrix,
                             precision_recall_curve, roc_auc_score, roc_curve)
from sklearn.model_selection import StratifiedKFold, cross_val_predict
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

FEATURES = [
    "rtt_med", "rtt_p95", "rtt_std", "rtt_min", "rtt_max", "rtt_slope",
    "loss_rate", "jitter_med", "rssi_med", "rssi_std", "rssi_slope",
    "valid_ratio", "rtt_delta",
]


# ----------------------------------------------------------------- data
def load_dataset(path: Path):
    by_trace: dict[str, dict] = {}
    with path.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            tr = row["trace"]
            d = by_trace.setdefault(tr, {"t": [], "X": [], "y": []})
            d["t"].append(float(row["t_s"]))
            d["X"].append([float(row[c]) if row[c] not in ("", "nan") else np.nan for c in FEATURES])
            d["y"].append(int(row["label"]))
    for d in by_trace.values():
        d["t"] = np.array(d["t"])
        d["X"] = np.array(d["X"], dtype=float)
        d["y"] = np.array(d["y"], dtype=int)
        order = np.argsort(d["t"])
        d["t"], d["X"], d["y"] = d["t"][order], d["X"][order], d["y"][order]
    return by_trace


def impute(Xtr, Xte):
    """用训练集列中位数填 NaN（不许用测试集信息）。"""
    med = np.nanmedian(Xtr, axis=0)
    med = np.where(np.isnan(med), 0.0, med)
    Xtr = np.where(np.isnan(Xtr), med, Xtr)
    Xte = np.where(np.isnan(Xte), med, Xte)
    return Xtr, Xte, med


# ----------------------------------------------------------------- metrics
def best_f1_threshold(y, p):
    prec, rec, thr = precision_recall_curve(y, p)
    f1 = np.divide(2 * prec * rec, prec + rec, out=np.zeros_like(prec), where=(prec + rec) > 0)
    k = int(np.argmax(f1))
    th = float(thr[k]) if k < len(thr) else 0.5
    return th, float(f1[k])


def recall_at_fpr(y, p, max_fpr=0.1):
    fpr, tpr, _ = roc_curve(y, p)
    ok = fpr <= max_fpr
    return float(tpr[ok].max()) if np.any(ok) else float("nan")


def eval_probs(y, p, th):
    yhat = (p >= th).astype(int)
    tn, fp, fn, tp = confusion_matrix(y, yhat, labels=[0, 1]).ravel()
    return {
        "n": int(len(y)),
        "pos_rate": round(float(np.mean(y)), 4),
        "auc": round(float(roc_auc_score(y, p)), 4) if len(np.unique(y)) > 1 else None,
        "pr_auc": round(float(average_precision_score(y, p)), 4) if len(np.unique(y)) > 1 else None,
        "threshold": round(th, 4),
        "f1": round(float(2 * tp / max(1, 2 * tp + fp + fn)), 4),
        "precision": round(float(tp / max(1, tp + fp)), 4),
        "recall": round(float(tp / max(1, tp + fn)), 4),
        "recall_at_fpr10": round(recall_at_fpr(y, p, 0.10), 4),
        "confusion": {"tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp)},
    }


def lead_time_events(t, y, p, th, max_lookback_s=5.0):
    """标签从 0 变 1 的事件，最早在多长时间前被预测到（秒）。"""
    leads, alarms = [], []
    prev = 0
    for i in range(len(y)):
        if y[i] == 1 and prev == 0:
            onset = t[i]
            j = i - 1
            first = None
            while j >= 0 and t[j] >= onset - max_lookback_s:
                if p[j] >= th:
                    first = t[j]
                j -= 1
            if first is not None:
                leads.append(onset - first)
        prev = y[i]
    # 误报：预测 1 但未来 1 s 内没有事件（粗略：连续段起点且标签为 0）
    prev_hat = 0
    for i in range(len(y)):
        hat = int(p[i] >= th)
        if hat == 1 and prev_hat == 0 and y[i] == 0:
            alarms.append(t[i])
        prev_hat = hat
    return leads, alarms


# ----------------------------------------------------------------- models
def make_models(seed: int):
    return {
        "logreg": make_pipeline(StandardScaler(),
                                LogisticRegression(max_iter=2000, class_weight="balanced",
                                                   random_state=seed)),
        "hgb": HistGradientBoostingClassifier(max_iter=200, learning_rate=0.08,
                                              max_depth=None, random_state=seed),
    }


def fit_model(name, model, Xtr, ytr, Xte):
    if name == "hgb":
        # 类别不平衡：给正例加权
        pos = max(1, int(ytr.sum()))
        neg = max(1, int(len(ytr) - ytr.sum()))
        w = np.where(ytr == 1, len(ytr) / (2 * pos), len(ytr) / (2 * neg))
        model.fit(Xtr, ytr, sample_weight=w)
    else:
        model.fit(Xtr, ytr)
    return model.predict_proba(Xte)[:, 1]


def tune_threshold(name, Xtr, ytr, seed):
    """用训练集上的 5 折 OOF 概率选阈值，避免在测试集上调参。"""
    model = make_models(seed)[name]
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=seed)
    try:
        oof = cross_val_predict(model, Xtr, ytr, cv=cv, method="predict_proba")[:, 1]
        th, f1 = best_f1_threshold(ytr, oof)
        return th, f1
    except Exception:
        return 0.5, 0.0


def baseline_scores(X, rtt_thresh: float, loss_thresh: float):
    idx = {f: i for i, f in enumerate(FEATURES)}
    rtt = X[:, idx["rtt_p95"]]
    med = X[:, idx["rtt_med"]]
    loss = X[:, idx["loss_rate"]]
    persistence = np.clip(np.nan_to_num(rtt, nan=0.0) / rtt_thresh, 0, 3) / 3.0
    rule = np.clip(np.maximum(np.nan_to_num(med, nan=0.0) / (0.8 * rtt_thresh),
                              np.nan_to_num(loss, nan=0.0) / max(1e-6, 0.5 * loss_thresh)), 0, 3) / 3.0
    return persistence, rule


# ----------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="训练链路风险预测器（监督学习）")
    ap.add_argument("--data", default="ml/artifacts/dataset.csv")
    ap.add_argument("--split", choices=["time", "trace"], default="time")
    ap.add_argument("--train-frac", type=float, default=0.7, help="time 划分时训练集占比")
    ap.add_argument("--gap", type=float, default=1.2, help="time 划分时训练/测试之间的隔离秒数")
    ap.add_argument("--rtt-thresh", type=float, default=80.0, help="与 build_dataset 保持一致")
    ap.add_argument("--loss-thresh", type=float, default=0.03)
    ap.add_argument("--out", default="ml/artifacts")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--no-plot", action="store_true")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    by_trace = load_dataset(Path(args.data))
    traces = sorted(by_trace)
    print(f"载入 {len(traces)} 条 trace，共 {sum(len(d['y']) for d in by_trace.values())} 样本")
    for name in traces:
        d = by_trace[name]
        print(f"  {name}: {len(d['y'])} 样本，正例 {d['y'].mean():.1%}，时长 {d['t'][-1]-d['t'][0]:.0f} s")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    results = {"split": args.split, "folds": [], "models": {}}
    test_preds = []   # (trace, t, y, p_model, p_persist, p_rule)

    folds = []
    if args.split == "trace":
        for name in traces:
            folds.append((name, [n for n in traces if n != name], [name]))
    else:
        for name in traces:
            d = by_trace[name]
            t = d["t"]
            cut = t[0] + args.train_frac * (t[-1] - t[0])
            tr = t < cut - args.gap
            te = t > cut + args.gap
            if tr.sum() < 30 or te.sum() < 30:
                print(f"  ! {name} 样本太少，跳过 time 划分")
                continue
            folds.append((name, [name], [name], tr, te))

    for fold in folds:
        if args.split == "trace":
            name, train_names, test_names = fold
            Xtr = np.vstack([by_trace[n]["X"] for n in train_names])
            ytr = np.concatenate([by_trace[n]["y"] for n in train_names])
            Xte = by_trace[name]["X"]
            yte = by_trace[name]["y"]
        else:
            name, _, _, tr_mask, te_mask = fold
            Xtr, ytr = by_trace[name]["X"][tr_mask], by_trace[name]["y"][tr_mask]
            Xte, yte = by_trace[name]["X"][te_mask], by_trace[name]["y"][te_mask]
            tte = by_trace[name]["t"][te_mask]
        if len(np.unique(ytr)) < 2:
            print(f"  ! {name} 训练集只有一类，跳过")
            continue

        Xtr_i, Xte_i, med_impute = impute(Xtr, Xte)
        if args.split == "trace":
            tte = by_trace[name]["t"]
        print(f"\n── fold {name}: train={len(ytr)} test={len(yte)} 正例率 {yte.mean():.1%}")

        # 基线
        p_persist, p_rule = baseline_scores(Xte_i, args.rtt_thresh, args.loss_thresh)
        quiet = bool(yte.mean() < 0.05)          # 近乎全程良好的 trace：看误报率，不看 AUC
        dur_min = max(1e-6, (tte[-1] - tte[0]) / 60.0)
        fold_res = {"fold": name, "quiet": quiet, "baselines": {}}
        for bname, p in (("persistence", p_persist), ("rule", p_rule)):
            th = 0.5
            fold_res["baselines"][bname] = eval_probs(yte, p, th)
            alarms = lead_time_events(tte, yte, p, th)[1]
            fold_res["baselines"][bname]["alarms_per_min"] = round(len(alarms) / dur_min, 2)
            print(f"   基线 {bname:12s} AUC={fold_res['baselines'][bname]['auc']} "
                  f"F1={fold_res['baselines'][bname]['f1']} "
                  f"Recall@FPR10={fold_res['baselines'][bname]['recall_at_fpr10']}")

        # 模型
        preds_models = {}
        for mname in ("logreg", "hgb"):
            th, _ = tune_threshold(mname, Xtr_i, ytr, args.seed)
            model = make_models(args.seed)[mname]
            p = fit_model(mname, model, Xtr_i, ytr, Xte_i)
            m = eval_probs(yte, p, th)
            m["threshold"] = round(th, 4)
            m["quiet"] = quiet
            alarms = lead_time_events(tte, yte, p, th)[1]
            m["alarms_per_min"] = round(len(alarms) / dur_min, 2)
            fold_res[mname] = m
            preds_models[mname] = p
            print(f"   模型 {mname:12s} AUC={m['auc']} PR-AUC={m['pr_auc']} "
                  f"F1={m['f1']} P={m['precision']} R={m['recall']} "
                  f"Recall@FPR10={m['recall_at_fpr10']} (th={th:.3f})")
            # 保存主模型
            if mname == "hgb":
                import joblib
                joblib.dump({"model": model, "features": FEATURES,
                             "impute_median": med_impute, "rtt_thresh": args.rtt_thresh,
                             "loss_thresh": args.loss_thresh, "threshold": th},
                            out / "model.joblib")

        if "hgb" in preds_models:
            test_preds.append((name, tte, yte, preds_models["hgb"],
                               p_persist, p_rule, Xte_i, quiet))
        results["folds"].append(fold_res)

    # 汇总（只在有事件的 fold 上比较 AUC/F1；静默 trace 单列误报率）
    for mname in ("persistence", "rule", "logreg", "hgb"):
        vals = []
        for f in results["folds"]:
            if f.get("quiet"):
                continue
            src = f.get(mname) or f["baselines"].get(mname)
            if src and src.get("auc") is not None:
                vals.append(src)
        if not vals:
            continue
        results["models"][mname] = {
            "auc_mean": round(float(np.mean([v["auc"] for v in vals])), 4),
            "f1_mean": round(float(np.mean([v["f1"] for v in vals])), 4),
            "recall_mean": round(float(np.mean([v["recall"] for v in vals])), 4),
            "pr_auc_mean": round(float(np.mean([v["pr_auc"] for v in vals])), 4),
            "positive_rate_mean": round(float(np.mean([v["pos_rate"] for v in vals])), 4),
        }

    print("\n══ 汇总（各 fold 平均）══")
    print(f"{'模型':14s} {'AUC':>7s} {'F1':>7s} {'Recall':>7s} {'PR-AUC':>7s}")
    for mname, v in results["models"].items():
        print(f"{mname:14s} {v['auc_mean']:7.4f} {v['f1_mean']:7.4f} "
              f"{v['recall_mean']:7.4f} {v['pr_auc_mean']:7.4f}")

    quiet_folds = [f for f in results["folds"] if f.get("quiet")]
    if quiet_folds:
        print("\n══ 静默 trace（几乎无事件）上的误报率 / 分钟 ══")
        for f in quiet_folds:
            parts = []
            for mname in ("persistence", "rule", "logreg", "hgb"):
                src = f.get(mname) or f["baselines"].get(mname)
                if src:
                    parts.append(f"{mname}={src.get('alarms_per_min', 0):.2f}")
            print(f"  {f['fold']}: " + "  ".join(parts))

    # 提前量（仅 trace 划分且能拿到时间轴时）
    if args.split == "trace" and test_preds:
        leads = []
        for item in test_preds:
            name, t, y, p, _, _, _, quiet_f = item
            if quiet_f:
                continue
            leads.extend(lead_time_events(t, y, p, 0.5)[0])
        if leads:
            results["lead_time_s"] = {"median": round(float(np.median(leads)), 2),
                                      "p90": round(float(np.percentile(leads, 90)), 2),
                                      "n_events": len(leads)}
            print(f"\n预测提前量：中位数 {results['lead_time_s']['median']} s "
                  f"｜P90 {results['lead_time_s']['p90']} s ｜事件 {len(leads)}")

    (out / "metrics.json").write_text(json.dumps(results, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    print(f"\n已保存：{out / 'metrics.json'}")
    print(f"        {out / 'model.joblib'}（hgb 主模型）")

    # 图
    if not args.no_plot and test_preds:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            try:
                plt.rcParams["font.family"] = "Microsoft YaHei"
                plt.rcParams["axes.unicode_minus"] = False
            except Exception:
                pass

            event_preds = [p for p in test_preds if not p[7]]
            yy = np.concatenate([p[2] for p in event_preds])
            pp = np.concatenate([p[3] for p in event_preds])
            fig, ax = plt.subplots(1, 2, figsize=(10, 4))
            fpr, tpr, _ = roc_curve(yy, pp)
            ax[0].plot(fpr, tpr, color="#2F5597", label=f"HGB AUC={roc_auc_score(yy, pp):.3f}")
            ax[0].plot([0, 1], [0, 1], "--", color="#999999", linewidth=1)
            ax[0].set_xlabel("假正率 FPR")
            ax[0].set_ylabel("真正率 TPR")
            ax[0].set_title("ROC")
            ax[0].legend(fontsize=8)
            ax[0].grid(alpha=0.3)
            prec, rec, _ = precision_recall_curve(yy, pp)
            ax[1].plot(rec, prec, color="#C55A11",
                       label=f"PR-AUC={average_precision_score(yy, pp):.3f}")
            ax[1].set_xlabel("召回率 Recall")
            ax[1].set_ylabel("精确率 Precision")
            ax[1].set_title("PR 曲线")
            ax[1].legend(fontsize=8)
            ax[1].grid(alpha=0.3)
            fig.tight_layout()
            fig.savefig(out / "roc_pr.png", dpi=160)
            plt.close(fig)

            # 时间序列（每个 fold 一条）
            n = len(test_preds)
            fig, axes = plt.subplots(n, 1, figsize=(10, 2.2 * n), sharex=False)
            if n == 1:
                axes = [axes]
            for ax, (name, t, y, p, pp_, pr_, _x, _q) in zip(axes, test_preds):
                t_rel = t - t[0]
                ax.fill_between(t_rel, 0, 1, where=y == 1, color="#FBE5D6",
                                step="mid", label="真实恶化")
                ax.plot(t_rel, p, color="#2F5597", linewidth=1.2, label="预测概率")
                ax.set_ylim(-0.02, 1.02)
                ax.set_ylabel("P(恶化)")
                ax.set_title(name, fontsize=9)
                ax.grid(alpha=0.3)
                ax.legend(fontsize=7, loc="upper right")
            axes[-1].set_xlabel("时间 (s)")
            fig.tight_layout()
            fig.savefig(out / "timeseries.png", dpi=160)
            plt.close(fig)
            print(f"已保存图：{out / 'roc_pr.png'}、{out / 'timeseries.png'}")
        except Exception as exc:
            print(f"（画图失败：{exc}）")

    # 特征重要性（在主模型上做 permutation，测试集子集）
    try:
        import joblib
        bundle = joblib.load(out / "model.joblib")
        if test_preds:
            Xte = np.vstack([p[6] for p in test_preds])
            yte = np.concatenate([p[2] for p in test_preds])
            med = np.asarray(bundle.get("impute_median"))
            Xte = np.where(np.isnan(Xte), med, Xte)
            imp = permutation_importance(bundle["model"], Xte, yte, n_repeats=5,
                                         random_state=args.seed, scoring="roc_auc")
            order = np.argsort(imp.importances_mean)[::-1][:8]
            print("\n特征重要性（AUC 下降）：")
            for i in order:
                print(f"  {FEATURES[i]:14s} {imp.importances_mean[i]:+.4f}")
    except Exception as exc:
        print(f"（特征重要性计算跳过：{exc}）")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
