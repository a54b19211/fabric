# 质量门禁标注集目录（T7 实证数据）

本目录存放 **D 组（贡桢玥）人工标注的质量门禁样本**，供 `scripts/quality_gate_eval.py`
评估「系统 T4 自动分级」与「专家人工判断」的一致性（论文第 5 章实证）。

> 📄 **标注同学请先读 [`标注规范.md`](./标注规范.md)**：含四级判据、抽样方案、
> 盲标纪律、查看与自检命令。本文件仅列 CSV 字段定义（工具视角）。

## 标注集文件

放置 `sample_labels.csv`（文件名可自定，运行时用 `--labels` 指定）：

| 列 | 必填 | 说明 |
|---|---|---|
| `uuid` | 是 | 文档 uuid，需与 `data/json_envelopes/<source>/<uuid>.json` 对应 |
| `source` | 是 | envelope 子目录名，如 `roche` / `pubmed` / `fda_api_drug_label` |
| `expert_level` | 是 | 专家判定等级，取值固定为 `高` / `中` / `低` / `隔离` |
| `expert_pass` | 否 | 门禁判定：`1`/`通过` 或 `0`/`隔离`。缺省时由 `expert_level != "隔离"` 自动推导 |

示例：

```csv
uuid,source,expert_level,expert_pass
f526f6c0-ec0b-5536-b434-7adc52aa357b,roche,中,1
b8ea5835-c5ea-5896-9cea-6dbebe751e07,pubmed,高,1
08fa6b79-95a1-5d55-8c50-f69994e4392a,fda_api_enforcement,隔离,0
```

> ⚠️ 在真实标注集就位前，**任何本目录生成的 κ / 混淆矩阵都只是方法演示，
> 不得作为论文实证结论**。脚本 `--self-test` 可单独验证指标逻辑，无需数据。

## 运行（真实评估）

```bash
python scripts/quality_gate_eval.py \
    --labels data/quality_annotations/sample_labels.csv \
    --envelope-dir data/json_envelopes \
    --out data/quality_annotations/eval_report.md
```

输出 `eval_report.md`：Cohen's κ（非加权 + 二次加权）、混淆矩阵、逐类 P/R/F1、
门禁二分类（通过/隔离）P/R/F1，可直接整理进论文第 5 章。

## 口径说明

- 本评估**只评 T4 质量分级这一环**（任务手册 T7 定义「跑 T4 系统分级，得系统判定」）。
- 脚本离线运行，**不触发 PubChem 联网**、不修改任何管线源码。
- 真值来自人工标注；系统分级来自 T4（`core_engine/quality_checker.check_quality`）。
