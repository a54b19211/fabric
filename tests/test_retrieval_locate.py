"""db.retrieval.locate：命中块定位回固定正文（v3 3.6 小批实测）。"""
from db.retrieval import locate

FIXED = "intro\n| a | b |\n| --- | --- |\n| 1 | x |\n| 2 | y |\n| 3 | z |\nend"


def test_exact_and_whitespace_insensitive_chunks():
    assert locate(FIXED, "| 1 | x |\n| 2 | y |") == (30, 49)
    assert locate(FIXED, "<!-- Fabric fixed version " + "a" * 64 + " -->\nintro") == (0, 5)
    assert locate(FIXED, "|  1 |  x |") is not None
    assert locate(FIXED, "not in the text") is None and locate(FIXED, "") is None


def test_table_chunks_with_a_repeated_header_locate_their_rows():
    """WeKnora 分长表格时每块前面重复表头：表头在正文里，定位表头之后的行。"""
    start, end = locate(FIXED, "| a | b |\n| --- | --- |\n| 3 | z |")
    assert FIXED[start:end] == "| 3 | z |"
    assert locate(FIXED, "| q | r |\n| --- | --- |\n| 3 | z |") is None  # 表头不在正文里：不认
