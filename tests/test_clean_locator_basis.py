"""清洗定位基准端到端可回读（Codex r64）：清洗段 → 存储的 segments → 契约 Locator → 回读原件重建片段。
覆盖网页原件（JSON 转义、多段、模板删除后断段）和投影正文两种基准。"""

import hashlib
import json
from datetime import datetime, timezone

from contracts import data_objects as do
from db import clean_stage
from processing import clean_text as ct

MARKDOWN = (
    '关于注销药品注册证书的公告\n\n'
    '国药准字H20234567 的注册证书予以注销。\n打印本页\n'
    '持有人"某药业"（含 \\ 反斜杠、制表\t符、emoji 😀）应于 30 日内交回证书。'
)
RAW = json.dumps({"schema_version": "datafabric.web_capture.v1", "role": "web_capture",
                  "html": "<html>archived \"HTML\"</html>",
                  "parsed": {"title": "公告", "markdown": MARKDOWN}}, ensure_ascii=False, sort_keys=True)
V = "a" * 64


def contract_for(row, result, basis):
    entry = {"version_id": V, "processor_version": ct.PROCESSOR_VERSION, "status": "parsed", "basis": basis,
             "text": result["text"], "text_sha256": hashlib.sha256(result["text"].encode()).hexdigest(),
             "segments": result["segments"], "language": result["language"], "locator_kind": "char_span"}
    return do.from_clean_text(entry, source_id=row["source_id"], record_id="r", seen=datetime.now(timezone.utc))


def check_every_segment(result, resolve):
    for seg in result["segments"]:
        loc = do.Locator(kind="json_pointer", version_id=V, json_pointer=seg["source"]["json_pointer"],
                         start=seg["source"]["start"], end=seg["source"]["end"], within=seg["source"]["within"])
        assert ct.normalize_whitespace(resolve(loc)) == result["text"][seg["start"]:seg["end"]]


def test_web_capture_segments_resolve_inside_the_decoded_markdown_field():
    row = {"source_id": "nmpa", "facts": {"role": "web_capture"}, "raw": RAW, "clean_text": None}
    inp, basis = clean_stage._cleaner_input(row)
    result = clean_stage._run_cleaner("nmpa", inp, basis)
    result = {**result, "segments": clean_stage._anchored(result["segments"], basis)}
    assert len(result["segments"]) >= 3  # 模板行（打印本页）处断段
    first = contract_for(row, result, basis).locators[0]
    assert (first.kind, first.json_pointer, first.within) == ("json_pointer", "/parsed/markdown", "raw")
    assert do.resolve(first, raw_content=RAW) == "关于注销药品注册证书的公告"  # 不是 {"html": "<ht
    check_every_segment(result, lambda loc: do.resolve(loc, raw_content=RAW))


def test_projection_segments_resolve_inside_the_version_record():
    # a source cleaned from its projection (WHO is cleaned from its raw JSON since v12, see below)
    projection = "通报：某地报告 3 例。\n\n“引号”与 \\ 都按解码后的字符计。"
    record = {"clean_text": projection}
    row = {"source_id": "legacy_documents", "facts": {}, "raw": None, "clean_text": projection}
    inp, basis = clean_stage._cleaner_input(row)
    result = clean_stage._run_cleaner("legacy_documents", inp, basis)
    result = {**result, "segments": clean_stage._anchored(result["segments"], basis)}
    first = contract_for(row, result, basis).locators[0]
    assert (first.json_pointer, first.within) == ("/clean_text", "record")
    check_every_segment(result, lambda loc: do.resolve(loc, raw_content="{}", record=record))


def test_reused_content_gets_the_basis_of_the_version_it_is_written_for():
    """真实数据里发现：库存冻结的 FDA 文档与站点插件的同一条记录正文相同，复用了后者的清洗结果，
    连带继承了“原件 /parsed/markdown”的定位基准。内容可以复用，定位要按当前版本重建。"""
    site = {"source_id": "fda_api_enforcement", "facts": {"role": "site_record"},
            "raw": json.dumps({"parsed": {"markdown": "# 召回\n\n正文一段。"}}, ensure_ascii=False), "clean_text": None}
    stored = {"source_id": "legacy_documents", "facts": {"source_site": "fda_api_enforcement"}, "raw": None,
              "clean_text": "# 召回\n\n正文一段。"}
    (a, basis_a), (b, basis_b) = clean_stage._cleaner_input(site), clean_stage._cleaner_input(stored)
    assert a == b and basis_a != basis_b  # 同样的清洗输入（会复用），不同的定位基准
    shared = clean_stage._run_cleaner("x", a, basis_a)
    one, two = clean_stage._anchored(shared["segments"], basis_a), clean_stage._anchored(shared["segments"], basis_b)
    assert {s["source"]["within"] for s in one} == {"raw"} and {s["source"]["within"] for s in two} == {"record"}
    assert all("within" not in s.get("source", {}) for s in shared["segments"])  # 共用的结果没被改


WHO_RAW = json.dumps({"Id": "28ac1b73-33a6-45fa-8474-6690de75b2ff", "Title": "Mpox - Democratic Republic of the Congo",
                      "Summary": "<p>Since 1 January, 1 444 suspected cases.</p>",
                      "Overview": "<p>By age:</p><table><tr><th>Age</th><th>Cases</th></tr>"
                                  "<tr><td>0-4</td><td>380</td></tr></table>"}, ensure_ascii=False)


def test_who_v12_segments_point_at_their_raw_field_and_block():
    """v12 cleans WHO from its raw JSON: each segment names the field (JSON pointer in the raw) and its block in
    that field; the field read back and cleaned again gives exactly that segment (no character span: HTML
    turned into text is not a substring of the raw)."""
    row = {"source_id": "who_don", "facts": {}, "raw": WHO_RAW, "clean_text": "flattened projection"}
    inp, basis = clean_stage._cleaner_input(row)
    assert basis == "who_raw_json"
    result = clean_stage._run_cleaner("who_don", inp, basis)
    result = {**result, "segments": clean_stage._anchored(result["segments"], basis)}
    first = contract_for(row, result, basis).locators[0]
    assert (first.kind, first.json_pointer, first.within, first.start) == ("json_pointer", "/Title", "raw", None)
    assert do.resolve(first, raw_content=WHO_RAW) == "Mpox - Democratic Republic of the Congo"
    for seg in result["segments"]:
        loc = do.Locator(kind="json_pointer", version_id=V, json_pointer=seg["source"]["json_pointer"],
                         within=seg["source"]["within"])
        field = do.resolve(loc, raw_content=WHO_RAW)
        text = result["text"][seg["start"] : seg["end"]]
        if seg["source"]["json_pointer"] == "/Title":
            assert text == "# " + field
        else:
            assert ct._who_blocks(field)[seg["source"]["block"]][1] == text
    assert "| Age | Cases |\n| --- | --- |\n| 0-4 | 380 |" in result["text"]
