"""适配示例：徐子雯的 T7 实证评估（她包里的 evaluation/*、scripts/quality_gate_eval.py）对着主线的打分跑。主线不调用。
她的评估只从 pipeline.quality_checker 取 _DIM_WEIGHTS、_score_dimensions、_score_completeness、_score_authority、
check_quality.fn；这里用 clean/quality.py 顶上（doc 的键和她的 parsed_result 一样）。scikit-learn 不在锁文件里，
quality_gate_eval 要在装了它的环境里跑。她的文件复制到一个目录（里面有 evaluation/、scripts/），再：
    python -I ops/xu_eval_adapter.py <那个目录> <一份资料 doc.json>
"""

import json
import sys
import types
from datetime import date
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), sys.argv[1]]
from clean import quality  # noqa: E402

TODAY = date.today()


def check_quality(parsed):
    got = quality.score(parsed, TODAY)
    return {**parsed, "quality_score": got["score"], "quality_level": got["level"], "quality_dimensions": got["dimensions"]}


shim = types.ModuleType("pipeline.quality_checker")
shim._DIM_WEIGHTS = quality.WEIGHTS
shim._score_dimensions = lambda parsed: quality.dimensions(parsed, TODAY)
shim._score_completeness = quality.completeness
shim._score_authority = lambda parsed: quality.dimensions(parsed, TODAY)["authority"]
shim.check_quality = types.SimpleNamespace(fn=check_quality)
sys.modules.update({"pipeline": types.ModuleType("pipeline"), "pipeline.quality_checker": shim})

if __name__ == "__main__":
    from evaluation import ablation, baselines, data_polluter  # 她的代码，原样

    doc = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    out = {"ablation": ablation.run_ablation(doc),
           "baselines": {name: round(baselines.score_baseline(doc, name), 4) for name in baselines.BASELINES},
           "polluted": {name: check_quality(data_polluter.pollute(doc, name, seed=1))["quality_score"]
                        for name in data_polluter.POLLUTERS}}
    try:
        sys.path.insert(0, str(Path(sys.argv[1]) / "scripts"))
        import quality_gate_eval as gate  # 要 scikit-learn

        out["gate"] = gate.system_level_and_score(doc)
        gate.self_test()
    except ImportError as exc:
        out["gate"] = f"没跑：{exc}"
    print(json.dumps(out, ensure_ascii=False, indent=1))
