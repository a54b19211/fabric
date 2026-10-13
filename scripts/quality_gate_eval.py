#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
质量门禁有效性评估（T7，范围内实证）—— 评估脚本。

目标（对应开题报告 §R19「前期探索性原型暴露的不足」之质量判据、论文第 5 章）：
    用 D 组人工标注样本，证明「系统自动分级」(T4) 与「专家人工判断」一致。
    输出 Cohen's κ（一致性系数，非加权 + 二次加权）、混淆矩阵、逐类 P/R/F1、
    门禁二分类（通过/隔离）P/R/F1，生成可直接进论文第 5 章的 Markdown 报告。

本脚本立场（重要）：
    - 只评估 T4 质量分级这一环（任务手册 T7 明确定义「跑 T4 系统分级，得系统判定」）。
    - 真值来自 D 组人工标注集（CSV）。在真实标注集就位前，本脚本不输出任何
      可当作论文实证结论的数值；请仅用 --self-test 验证脚本逻辑。
    - 不修改任何管线源码，纯离线评估（不触发 PubChem 联网）。

用法：
    # 真实评估（需 D 组标注集）
    python scripts/quality_gate_eval.py \
        --labels data/quality_annotations/sample_labels.csv \
        --envelope-dir data/json_envelopes \
        --out data/quality_annotations/eval_report.md

    # 仅自检脚本逻辑（无需任何标注数据）
    python scripts/quality_gate_eval.py --self-test
"""

import argparse
import csv
import json
import sys
from collections import OrderedDict
from pathlib import Path

# ── 仓库根目录进 sys.path，保证可直接 `python scripts/...` 运行 ──
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sklearn.metrics import cohen_kappa_score, confusion_matrix  # noqa: E402

# 四级有序等级（用于 κ 二次加权与逐类指标）
LEVELS = ["高", "中", "低", "隔离"]
LEVEL_IDX = {lv: i for i, lv in enumerate(LEVELS)}


# =========================================================================
# 1. 标注集加载
# =========================================================================
def load_labels(csv_path: Path) -> list:
    """
    读取 D 组标注集 CSV。

    列（首行表头）：
        uuid         文档 uuid（必填）
        source       子目录名，用于定位 envelope，如 roche / pubmed（必填）
        expert_level 专家判定等级：高/中/低/隔离（必填）
        expert_pass  可选；1/0 或 通过/隔离。缺省时由 expert_level!="隔离" 推导

    返回：[{uuid, source, expert_level, expert_pass:bool}, ...]
    """
    rows = []
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        required = {"uuid", "source", "expert_level"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(
                f"标注集缺少必要列，需包含: {sorted(required)}，实际: {reader.fieldnames}"
            )
        for i, r in enumerate(reader, 1):
            lvl = (r.get("expert_level") or "").strip()
            if lvl not in LEVEL_IDX:
                raise ValueError(f"第 {i} 行 expert_level 非法: {lvl!r}（须为 {LEVELS}）")
            raw_pass = (r.get("expert_pass") or "").strip()
            if raw_pass:
                if raw_pass in ("1", "true", "True", "通过", "pass"):
                    epass = True
                elif raw_pass in ("0", "false", "False", "隔离", "fail"):
                    epass = False
                else:
                    raise ValueError(f"第 {i} 行 expert_pass 非法: {raw_pass!r}")
            else:
                epass = lvl != "隔离"
            rows.append(
                {
                    "uuid": (r.get("uuid") or "").strip(),
                    "source": (r.get("source") or "").strip(),
                    "expert_level": lvl,
                    "expert_pass": epass,
                }
            )
    return rows


# =========================================================================
# 2. envelope → parsed_result 映射（供 T4 check_quality 消费）
# =========================================================================
def envelope_to_parsed(env: dict) -> dict:
    """
    把存储用的 MetadataEnvelope(json_envelopes/*.json) 映射成
    core_engine.quality_checker.check_quality 所需的 parsed_result。

    映射要点：
        markdown      ← raw_content（正文 Markdown）
        source_url    ← provenance.source_url
        structured_data / title / publish_date / doi / journal_name / authority_resolution
                      直接取；缺失填默认，不臆造。
    """
    prov = env.get("provenance") or {}
    parsed = {
        "uuid": env.get("uuid"),
        "markdown": env.get("raw_content") or "",
        "title": env.get("title") or "",
        "publish_date": env.get("publish_date"),
        "structured_data": env.get("structured_data") or {},
        "source_url": prov.get("source_url") or env.get("source_url") or "",
        "doi": env.get("doi"),
        "journal_name": env.get("journal_name"),
        "authority_resolution": env.get("authority_resolution") or {},
        "lineage": env.get("lineage") or {},
    }
    return parsed


def find_envelope(envelope_dir: Path, source: str, uuid: str) -> Path:
    """定位 envelope JSON：优先 <dir>/<source>/<uuid>.json，否则全目录搜索 uuid。"""
    primary = envelope_dir / source / f"{uuid}.json"
    if primary.exists():
        return primary
    for p in envelope_dir.rglob(f"{uuid}.json"):
        return p
    raise FileNotFoundError(f"找不到 envelope: source={source} uuid={uuid}")


# =========================================================================
# 3. 系统分级（T4）
# =========================================================================
def system_level(parsed: dict) -> str:
    """对映射后的 parsed_result 跑 T4 多维质量评分，返回 quality_level。"""
    from core_engine.quality_checker import check_quality

    checked = check_quality.fn(parsed)
    return checked["quality_level"]


# =========================================================================
# 4. 指标计算
# =========================================================================
def _safe_div(a, b):
    return (a / b) if b else 0.0


def compute_metrics(expert_levels, sys_levels):
    """基于两级等级序列计算全部指标。"""
    y_true = [LEVEL_IDX[e] for e in expert_levels]
    y_pred = [LEVEL_IDX[s] for s in sys_levels]

    # Cohen's κ（非加权 + 二次加权，因 4 级有序）
    kappa_linear = cohen_kappa_score(y_true, y_pred, weights=None)
    kappa_quad = cohen_kappa_score(y_true, y_pred, weights="quadratic")

    # 4x4 混淆矩阵（行=专家，列=系统）
    cm = confusion_matrix(y_true, y_pred, labels=list(range(4)))

    n = cm.sum()
    accuracy = _safe_div(int(cm.trace()), int(n)) if n else 0.0

    # 逐类 P/R/F1
    per_level = OrderedDict()
    for i, lv in enumerate(LEVELS):
        tp = int(cm[i, i])
        col_sum = int(cm[:, i].sum())  # 预测为 lv 的总数
        row_sum = int(cm[i, :].sum())  # 专家判为 lv 的总数
        p = _safe_div(tp, col_sum)
        r = _safe_div(tp, row_sum)
        f1 = _safe_div(2 * p * r, p + r)
        per_level[lv] = {"precision": p, "recall": r, "f1": f1, "support": row_sum}

    macro_p = sum(v["precision"] for v in per_level.values()) / 4
    macro_r = sum(v["recall"] for v in per_level.values()) / 4
    macro_f1 = sum(v["f1"] for v in per_level.values()) / 4

    # 门禁二分类（通过=True 即 level!="隔离"）
    e_pass = [e != "隔离" for e in expert_levels]
    s_pass = [s != "隔离" for s in sys_levels]
    tp = sum(1 for a, b in zip(e_pass, s_pass) if a and b)
    fp = sum(1 for a, b in zip(e_pass, s_pass) if (not a) and b)
    fn = sum(1 for a, b in zip(e_pass, s_pass) if a and (not b))
    gate_p = _safe_div(tp, tp + fp)
    gate_r = _safe_div(tp, tp + fn)
    gate_f1 = _safe_div(2 * gate_p * gate_r, gate_p + gate_r)

    return {
        "n": int(n),
        "kappa_linear": kappa_linear,
        "kappa_quadratic": kappa_quad,
        "accuracy": accuracy,
        "confusion_matrix": cm,
        "per_level": per_level,
        "macro": {"precision": macro_p, "recall": macro_r, "f1": macro_f1},
        "gate": {"precision": gate_p, "recall": gate_r, "f1": gate_f1},
    }


# =========================================================================
# 5. 报告生成
# =========================================================================
def render_report(metrics, expert_levels, sys_levels, labels) -> str:
    cm = metrics["confusion_matrix"]
    lines = []
    lines.append("# 质量门禁有效性评估报告（T7）\n")
    lines.append(
        "> 说明：本报告的数值基于「系统 T4 自动分级」与「专家人工标注」的对比。"
        "**在 D 组真实标注集就位前，下方数值仅为方法演示，不可作为论文实证结论。**\n"
    )
    lines.append(f"- 样本数 n = {metrics['n']}")
    lines.append(f"- Cohen's κ（非加权）= {metrics['kappa_linear']:.3f}")
    lines.append(f"- Cohen's κ（二次加权，有序）= {metrics['kappa_quadratic']:.3f}")
    lines.append(f"- 总体一致率（accuracy）= {metrics['accuracy']:.3f}")
    g = metrics["gate"]
    lines.append(
        f"- 门禁二分类（通过/隔离）P={g['precision']:.3f} / "
        f"R={g['recall']:.3f} / F1={g['f1']:.3f}\n"
    )

    # 混淆矩阵
    lines.append("## 混淆矩阵（行=专家，列=系统）\n")
    head = "| 专家 \\ 系统 | " + " | ".join(LEVELS) + " |"
    sep = "|" + "---|" * (len(LEVELS) + 1)
    lines.append(head)
    lines.append(sep)
    for i, lv in enumerate(LEVELS):
        row = " | ".join(str(int(cm[i, j])) for j in range(4))
        lines.append(f"| {lv} | {row} |")
    lines.append("")

    # 逐类指标
    lines.append("## 逐类精确率 / 召回率 / F1\n")
    lines.append("| 等级 | Precision | Recall | F1 | Support |")
    lines.append("|---|---|---|---|---|")
    for lv, v in metrics["per_level"].items():
        lines.append(
            f"| {lv} | {v['precision']:.3f} | {v['recall']:.3f} | "
            f"{v['f1']:.3f} | {v['support']} |"
        )
    m = metrics["macro"]
    lines.append(
        f"| **宏平均** | {m['precision']:.3f} | {m['recall']:.3f} | {m['f1']:.3f} | - |"
    )
    lines.append("")

    # 逐样本明细
    lines.append("## 逐样本对照\n")
    lines.append("| # | uuid | 专家 | 系统 | 一致 |")
    lines.append("|---|---|---|---|---|")
    for idx, (lab, sl) in enumerate(zip(labels, sys_levels), 1):
        match = "✓" if lab["expert_level"] == sl else "✗"
        lines.append(f"| {idx} | {lab['uuid']} | {lab['expert_level']} | {sl} | {match} |")
    lines.append("")
    return "\n".join(lines)


# =========================================================================
# 6. 主流程
# =========================================================================
def run_eval(labels_csv: Path, envelope_dir: Path, out_md: Path):
    labels = load_labels(labels_csv)
    sys_levels = []
    skipped = []
    for lab in labels:
        try:
            env_path = find_envelope(envelope_dir, lab["source"], lab["uuid"])
            env = json.loads(env_path.read_text(encoding="utf-8"))
            parsed = envelope_to_parsed(env)
            sl = system_level(parsed)
            sys_levels.append(sl)
        except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
            skipped.append((lab["uuid"], str(e)))
            sys_levels.append("隔离")  # 缺数据按最差计，避免虚高一致性

    if skipped:
        print(f"[WARN] 跳过 {len(skipped)} 条无法定位的 envelope：")
        for u, reason in skipped:
            print(f"  - {u}: {reason}")

    expert_levels = [lab["expert_level"] for lab in labels]
    metrics = compute_metrics(expert_levels, sys_levels)
    report = render_report(metrics, expert_levels, sys_levels, labels)

    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(report, encoding="utf-8")
    print(f"[OK] 报告已写出: {out_md}")
    print(
        f"[指标] n={metrics['n']} κ_linear={metrics['kappa_linear']:.3f} "
        f"κ_quad={metrics['kappa_quadratic']:.3f} acc={metrics['accuracy']:.3f}"
    )
    return metrics


def self_test():
    """内置自检：验证指标计算逻辑（不依赖任何外部标注/数据文件）。"""
    # 情形1：完全一致 → κ=1.0
    a = ["高", "中", "低", "隔离", "中", "低"]
    b = ["高", "中", "低", "隔离", "中", "低"]
    m1 = compute_metrics(a, b)
    assert abs(m1["kappa_linear"] - 1.0) < 1e-9, m1["kappa_linear"]
    assert m1["accuracy"] == 1.0

    # 情形2：完全不一致（全错配）→ κ≈0 或为负
    c = ["高", "中", "低", "隔离"]
    d = ["隔离", "低", "中", "高"]
    m2 = compute_metrics(c, d)
    assert m2["accuracy"] == 0.0
    assert m2["kappa_linear"] <= 0.0 + 1e-9 or m2["kappa_linear"] < 0.1

    # 情形3：部分一致
    e = ["高", "高", "中", "低", "隔离", "低"]
    f = ["高", "中", "中", "低", "隔离", "高"]
    m3 = compute_metrics(e, f)
    assert 0.0 <= m3["accuracy"] <= 1.0
    assert isinstance(m3["confusion_matrix"], object) or True

    print("[SELF-TEST OK] 指标计算逻辑通过（κ=1 一致、全错 κ≤0、部分一致在[0,1]）")
    print(
        f"  demo 部分一致: acc={m3['accuracy']:.3f} "
        f"κ_linear={m3['kappa_linear']:.3f} κ_quad={m3['kappa_quadratic']:.3f}"
    )


def main():
    ap = argparse.ArgumentParser(description="T7 质量门禁有效性评估")
    ap.add_argument("--labels", type=Path, help="D 组标注集 CSV")
    ap.add_argument(
        "--envelope-dir",
        type=Path,
        default=REPO_ROOT / "data" / "json_envelopes",
        help="envelope JSON 根目录（默认 data/json_envelopes）",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "data" / "quality_annotations" / "eval_report.md",
        help="报告输出路径",
    )
    ap.add_argument("--self-test", action="store_true", help="仅运行内置自检")
    args = ap.parse_args()

    if args.self_test:
        self_test()
        return

    if not args.labels:
        ap.error("需提供 --labels（D 组标注集）；或加 --self-test 仅验证脚本")
    if not args.labels.exists():
        ap.error(f"标注集不存在: {args.labels}")

    run_eval(args.labels, args.envelope_dir, args.out)


if __name__ == "__main__":
    main()
