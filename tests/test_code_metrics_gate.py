"""The function-size gate (ops/code_metrics.py): a new violator, a stale entry and a frozen function that grew
all fail; the checked-in exceptions file matches the sources today."""
import importlib.util
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("code_metrics", ROOT / "ops" / "code_metrics.py")
metrics = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(metrics)


def rows_of(tmp_path, body_lines=0, branches=0):
    """Measure one function `f` in a.py with body_lines filler lines and `branches` if-statements."""
    src = "def f(x):\n" + "    y = 1\n" * body_lines + "    if x:\n        pass\n" * branches + "    return x\n"
    path = tmp_path / "a.py"
    path.write_text(src)
    return [("a.py", name, line, size, cc) for name, line, size, cc in metrics.functions(path)]


def test_a_function_within_the_limits_passes(tmp_path):
    assert metrics.problems_of(rows_of(tmp_path, body_lines=10), {}) == []


def test_a_new_long_or_complex_function_fails(tmp_path):
    long = metrics.problems_of(rows_of(tmp_path, body_lines=metrics.MAX_LINES), {})
    assert len(long) == 1 and "a.py:f" in long[0] and "no reason" in long[0]
    complex_ = metrics.problems_of(rows_of(tmp_path, branches=metrics.MAX_CC), {})
    assert len(complex_) == 1 and "complexity 21" in complex_[0]


def test_a_listed_function_that_is_within_the_limits_must_be_removed(tmp_path):
    problems = metrics.problems_of(rows_of(tmp_path, body_lines=5), {"a.py:f": (90, 3, "reason")})
    assert len(problems) == 1 and "remove the entry" in problems[0]
    assert len(metrics.problems_of([], {"a.py:gone": (90, 3, "reason")})) == 1  # a deleted function too


def test_a_listed_function_may_shrink_but_not_grow(tmp_path):
    rows = rows_of(tmp_path, body_lines=metrics.MAX_LINES + 10)  # 93 lines, cc 1
    size = rows[0][3]
    assert metrics.problems_of(rows, {"a.py:f": (size, 1, "reason")}) == []
    assert metrics.problems_of(rows, {"a.py:f": (size + 20, 1, "reason")}) == []  # smaller than frozen: fine
    grown = metrics.problems_of(rows, {"a.py:f": (size - 1, 1, "reason")})
    assert len(grown) == 1 and "grew" in grown[0]


def test_the_checked_in_list_matches_the_sources():
    _, rows = metrics.collect([ROOT / p for p in metrics.DEFAULT_PATHS])
    assert metrics.problems_of(rows, metrics.exceptions()) == []
    assert all(reason for _, _, reason in metrics.exceptions().values())
