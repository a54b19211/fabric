"""processing.index_pieces：长清洗产物的索引分片（v3 3.6，Codex 448/451）。小上限，便于覆盖各种切法。"""
import hashlib

from processing import index_pieces as ip

LIMIT = 300 + ip.UPLOAD_OVERHEAD  # 每片最多 300 字（再加上传附加的一行）


def check(text, pieces, max_chars=LIMIT, max_bytes=10 ** 9):
    assert pieces, "should be split"
    assert pieces[0]["start"] == 0 and pieces[-1]["end"] == len(text)
    for a, b in zip(pieces, pieces[1:]):
        assert a["end"] == b["start"]  # 首尾相接：每个字都在且只在一片里
    for p in pieces:
        body = ip.content(text, p)
        assert body.endswith(text[p["start"]:p["end"]]) and len(body) == p["chars"]
        assert p["chars"] + ip.UPLOAD_OVERHEAD <= max_chars and p["bytes"] + ip.UPLOAD_OVERHEAD <= max_bytes
        assert p["sha256"] == hashlib.sha256(body.encode()).hexdigest()
        assert p["rule"] == ip.RULE and p["count"] == len(pieces)
    assert [p["index"] for p in pieces] == list(range(1, len(pieces) + 1))


def test_piece_key_is_rule_index_range_and_digest():
    """Codex 456/460：唯一键用的分片身份键；不分片为空串，计划的键集合不分片时是 {\"\"}。"""
    text = "\n".join("line %03d " % i + "y" * 60 for i in range(40))
    pieces = ip.plan(text, [], max_chars=LIMIT)
    p = pieces[0]
    assert ip.key(p) == "%s:1:0-%d:%s" % (ip.RULE, p["end"], p["sha256"]) and ip.key(None) == ""
    assert len({ip.key(q) for q in pieces}) == len(pieces)
    assert len({ip.key(p), ip.key({**p, "end": p["end"] - 1}), ip.key({**p, "rule": "other"})}) == 3
    assert ip.keys("short text") == {""}


def test_short_text_is_not_split():
    assert ip.plan("short text", [], max_chars=LIMIT) == []


def test_long_text_splits_at_paragraph_boundaries_first():
    paragraphs = ["paragraph %d " % i + "x" * 90 for i in range(12)]
    text = "\n".join(paragraphs)
    segments, pos = [], 0
    for p in paragraphs:
        segments.append({"start": pos, "end": pos + len(p)})
        pos += len(p) + 1
    pieces = ip.plan(text, segments, max_chars=LIMIT)
    check(text, pieces)
    starts = {s["start"] for s in segments}
    assert all(p["start"] in starts for p in pieces)


def test_a_long_paragraph_splits_by_line_then_a_long_line_by_characters():
    text = "\n".join("line %02d " % i + "y" * 60 for i in range(20)) + "\n" + "word " * 200
    pieces = ip.plan(text, [{"start": 0, "end": len(text)}], max_chars=LIMIT)
    check(text, pieces)
    assert any(text[p["start"] - 1] == " " for p in pieces[1:])  # 长行在空白处切


def test_a_piece_starting_inside_a_table_carries_the_header_as_context():
    header = "| drug | arm | n |\n| --- | --- | --- |\n"
    rows = "".join("| drug %03d | arm A | %d |\n" % (i, i) for i in range(40))
    text = "intro\n" + header + rows + "end"
    pieces = ip.plan(text, [], max_chars=LIMIT)
    check(text, pieces)
    inside = [p for p in pieces if p["start"] > len("intro\n" + header) and text.startswith("|", p["start"])]
    assert inside and all(p["context_chars"] == len(header) for p in inside)
    assert all(ip.content(text, p).startswith(header) for p in inside)
    assert pieces[0]["context_chars"] == 0


def test_bytes_bind_for_chinese_text():
    text = "\n".join("第%d段：" % i + "药品抽检不合格" * 10 for i in range(30))
    pieces = ip.plan(text, [], max_chars=10 ** 6, max_bytes=LIMIT * 2)
    check(text, pieces, max_chars=10 ** 6, max_bytes=LIMIT * 2)
