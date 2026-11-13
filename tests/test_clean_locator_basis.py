"""清洗定位基准端到端可回读（Codex r64）：清洗段 → 存储的 segments → 按定位（JSON 路径 + 偏移）回读原件重建片段。
覆盖网页原件（JSON 转义、多段、模板删除后断段）和投影正文两种基准。"""

import json

from clean import clean_stage
from clean import clean_text as ct

MARKDOWN = (
    '关于注销药品注册证书的公告\n\n'
    '国药准字H20234567 的注册证书予以注销。\n打印本页\n'
    '持有人"某药业"（含 \\ 反斜杠、制表\t符、emoji 😀）应于 30 日内交回证书。'
)
RAW = json.dumps({"schema_version": "datafabric.web_capture.v1", "role": "web_capture",
                  "html": "<html>archived \"HTML\"</html>",
                  "parsed": {"title": "公告", "markdown": MARKDOWN}}, ensure_ascii=False, sort_keys=True)

def resolve(source, raw_content, record=None):
    """The text a segment's locator points at: its JSON pointer in the raw JSON (within raw) or in the version record
    (within record), cut to start/end when it has them."""
    node = json.loads(raw_content) if source.get("within", "raw") == "raw" else record
    for token in source["json_pointer"].split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        node = node[int(token)] if isinstance(node, list) else node[token]
    return node if source.get("start") is None else node[source["start"]:source["end"]]


def check_every_segment(result, read):
    for seg in result["segments"]:
        assert ct.normalize_whitespace(read(seg["source"])) == result["text"][seg["start"]:seg["end"]]


def test_web_capture_segments_resolve_inside_the_decoded_markdown_field():
    row = {"source_id": "nmpa", "facts": {"role": "web_capture"}, "raw": RAW, "clean_text": None}
    inp, basis = clean_stage._cleaner_input(row)
    result = clean_stage._clean_by_basis("nmpa", inp, basis)
    result = {**result, "segments": clean_stage._anchored(result["segments"], basis)}
    assert len(result["segments"]) >= 3  # 模板行（打印本页）处断段
    first = result["segments"][0]["source"]
    assert (first["json_pointer"], first["within"]) == ("/parsed/markdown", "raw")
    assert resolve(first, RAW) == "关于注销药品注册证书的公告"  # 不是 {"html": "<ht
    check_every_segment(result, lambda source: resolve(source, RAW))


def test_projection_segments_resolve_inside_the_version_record():
    # a source cleaned from its projection (WHO is cleaned from its raw JSON since v12, see below)
    projection = "通报：某地报告 3 例。\n\n“引号”与 \\ 都按解码后的字符计。"
    record = {"clean_text": projection}
    row = {"source_id": "legacy_documents", "facts": {}, "raw": None, "clean_text": projection}
    inp, basis = clean_stage._cleaner_input(row)
    result = clean_stage._clean_by_basis("legacy_documents", inp, basis)
    result = {**result, "segments": clean_stage._anchored(result["segments"], basis)}
    first = result["segments"][0]["source"]
    assert (first["json_pointer"], first["within"]) == ("/clean_text", "record")
    check_every_segment(result, lambda source: resolve(source, "{}", record))


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
    result = clean_stage._clean_by_basis("who_don", inp, basis)
    result = {**result, "segments": clean_stage._anchored(result["segments"], basis)}
    first = result["segments"][0]["source"]
    assert (first["json_pointer"], first["within"], first.get("start")) == ("/Title", "raw", None)
    assert resolve(first, WHO_RAW) == "Mpox - Democratic Republic of the Congo"
    for seg in result["segments"]:
        field = resolve({"json_pointer": seg["source"]["json_pointer"], "within": seg["source"]["within"]}, WHO_RAW)
        text = result["text"][seg["start"] : seg["end"]]
        if seg["source"]["json_pointer"] == "/Title":
            assert text == "# " + field
        else:
            assert ct._who_blocks(field)[seg["source"]["block"]][1] == text
    assert "| Age | Cases |\n| --- | --- |\n| 0-4 | 380 |" in result["text"]
