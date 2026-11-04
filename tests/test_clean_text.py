"""加工层正文清洗（processing.clean_text）：只删模板/导航整行、只规范空白；
否定、单位、限定词、更正声明一个字都不能丢；每段都能回指原件（字符偏移或 JSON pointer）；
没有正文就如实说没有，不补写。纯函数，不联网、不碰库。"""

import json

import pytest

from processing import clean_text as ct

KEY_TERMS_ZH = ("【更正】未见明显肝毒性（ALT 升高 ≥3×ULN 的发生率 2.1%），剂量 10 mg/kg，"
                "约 30% 疑似无显著获益，不推荐。")
KEY_TERMS_EN = ("Corrected: no significant difference (p = 0.21); not retracted; 5 μg/mL; "
                "≥ 18 years; approximately 12% suspected.")
MUST_SURVIVE = ["未见", "不推荐", "无显著", "mg/kg", "%", "≥", "约", "疑似", "【更正】", "2.1%", "≥3×ULN",
                "no significant", "not retracted", "μg/mL", "Corrected", "p = 0.21"]


def must_survive(text):
    for term in MUST_SURVIVE:
        if term in KEY_TERMS_ZH or term in KEY_TERMS_EN:
            assert term in text, term


def assert_shape(out):
    assert out["processor_version"] == ct.PROCESSOR_VERSION == ct.PROCESSOR_VERSION
    assert set(out) == {"processor_version", "text", "segments", "language", "removed_lines", "warnings"}
    for seg in out["segments"]:
        assert 0 <= seg["start"] < seg["end"] <= len(out["text"])
    # 段与段之间只隔分隔符，不会有漏掉或重叠的正文
    assert [s["start"] for s in out["segments"]] == sorted(s["start"] for s in out["segments"])


def seg_text(out, seg):
    return out["text"][seg["start"]:seg["end"]]


# ---------------------------------------------------------------- markdown

PAGE = """首页 > 新闻中心 > 药品监管
【字体：大 中 小】  打印本页   关闭窗口

# 关于某药品说明书的更正公告

【更正】原公告中　“剂量 10  mg/kg”\t应为“剂量 5 mg/kg”。
未见明显肝毒性（ALT 升高 ≥3×ULN 的发生率 2.1%），约 30% 疑似无显著获益，不推荐。
扫一扫在手机打开当前页

- 不良反应 发生率 ≥ 1%
- 撤回 原第 3 条

| 指标 | 结果 |
|---|---|
| ALT | 未见升高 |

<table>
<tr><td>首页</td><td>打印本页</td></tr>

<tr><td>剂量</td><td>10 mg/kg</td></tr>
</table>

请返回首页查看原公告全文，本页不再更新。
""" + KEY_TERMS_EN + """

分享到：微信 微博
上一篇：关于某某的通知
下一篇：无
— 3 —
Skip to main content
This site uses cookies. Accept all cookies
"""


def test_markdown_shape_and_key_terms_survive():
    out = ct.clean_markdown(PAGE, source_id="nmpa")
    assert_shape(out)
    must_survive(out["text"])
    assert "【更正】原公告中 “剂量 10 mg/kg” 应为“剂量 5 mg/kg”。" in out["text"]
    assert out["warnings"] == []


def test_markdown_boilerplate_removed_but_content_mentioning_template_words_kept():
    out = ct.clean_markdown(PAGE)
    text = out["text"]
    for junk in ["首页 > 新闻中心", "【字体", "扫一扫", "分享到", "上一篇", "下一篇", "— 3 —",
                 "Skip to main content", "Accept all cookies"]:
        assert junk not in text, junk
    assert "请返回首页查看原公告全文，本页不再更新。" in text
    assert out["removed_lines"] == 9


@pytest.mark.parametrize("line", [
    "首页 > 政务公开 > 通知公告", "首页>新闻", "当前位置：首页 > 公告", "打印本页", "【打印本页】",
    "关闭窗口",
    "[关闭窗口]", "分享到：", "分享到： 微信 QQ空间", "【字体：大 中 小】", "字体：[大] [中] [小]",
    "扫一扫在手机打开当前页", "上一篇：无", "下一篇: 关于X的公告", "Skip to main content", "— 3 —", "- 12 -",
    "第 3 页 共 10 页", "We use cookies to improve your experience. Accept", "Cookie Settings",
])
def test_each_boilerplate_line_alone_is_dropped(line):
    out = ct.clean_markdown(line + "\n\n正文：未见异常。")
    assert out["text"] == "正文：未见异常。", line
    assert out["removed_lines"] == 1


@pytest.mark.parametrize("line", [
    "首页 > 新闻 发布了不良反应通报，未见死亡病例。",
    "患者自行打印本页并关闭窗口后离开，未见不适。",
    "上一篇研究显示无显著差异。",
    "Cookie-cutter dosing (10 mg/kg) was not used.",
    "- 3 例患者出现 ≥2 级不良事件",
    "3",
])
def test_content_lines_with_template_words_are_kept(line):
    out = ct.clean_markdown(line)
    assert out["text"] == line
    assert out["removed_lines"] == 0


def test_markdown_offsets_round_trip():
    out = ct.clean_markdown(PAGE)
    assert out["segments"]
    for seg in out["segments"]:
        src = seg["source"]
        assert ct.normalize_whitespace(PAGE[src["start"]:src["end"]]) == seg_text(out, seg)


def test_markdown_offsets_round_trip_with_crlf():
    raw = "打印本页\r\n# 标题\r\n\r\n第一行　不良\r\n第二行  无显著\r\n"
    out = ct.clean_markdown(raw)
    assert out["text"] == "# 标题\n\n第一行 不良\n第二行 无显著"
    for seg in out["segments"]:
        src = seg["source"]
        assert ct.normalize_whitespace(raw[src["start"]:src["end"]]) == seg_text(out, seg)


def test_markdown_kinds_and_tables_not_split():
    out = ct.clean_markdown(PAGE)
    kinds = [s["kind"] for s in out["segments"]]
    assert kinds == ["heading", "paragraph", "list", "table", "table", "paragraph"]
    pipe, html = [seg_text(out, s) for s in out["segments"] if s["kind"] == "table"]
    assert pipe == "| 指标 | 结果 |\n|---|---|\n| ALT | 未见升高 |"
    # HTML 表格里有空行、还有“首页”“打印本页”单元格：整块保留，不切开也不删行
    assert html.startswith("<table>") and html.endswith("</table>")
    assert "<tr><td>首页</td><td>打印本页</td></tr>" in html and "10 mg/kg" in html


def test_markdown_boilerplate_line_inside_block_splits_segment_but_keeps_content():
    raw = "第一段 未见异常\n打印本页\n第二行 不推荐"
    out = ct.clean_markdown(raw)
    assert out["removed_lines"] == 1
    assert out["text"] == "第一段 未见异常\n\n第二行 不推荐"
    for seg in out["segments"]:
        src = seg["source"]
        assert ct.normalize_whitespace(raw[src["start"]:src["end"]]) == seg_text(out, seg)


def test_markdown_heading_directly_followed_by_paragraph():
    out = ct.clean_markdown("## 结果\n未见明显肝毒性。")
    assert [(s["kind"], seg_text(out, s)) for s in out["segments"]] == [
        ("heading", "## 结果"), ("paragraph", "未见明显肝毒性。")]


def test_markdown_does_not_change_other_characters():
    raw = "ＡＢＣ（全角）“引号” Ｐ＜0.05，CamelCase ＴＮＦ-α"
    assert ct.clean_markdown(raw)["text"] == raw


@pytest.mark.parametrize("raw", ["", "   \n　\n", "首页 > 公告\n打印本页\n关闭窗口\n— 2 —"])
def test_markdown_no_body(raw):
    out = ct.clean_markdown(raw)
    assert out["text"] == "" and out["segments"] == []
    assert out["warnings"] == ["no_body"]
    assert out["language"] == "unknown"


# ---------------------------------------------------------------- language

def test_language_detection():
    assert ct.detect_language(KEY_TERMS_ZH) == "zh"
    assert ct.detect_language(KEY_TERMS_EN) == "en"
    both = KEY_TERMS_ZH + "\n" + KEY_TERMS_EN + " The primary endpoint was not met."
    assert ct.detect_language(both) == "mixed"
    assert ct.detect_language("") == "unknown"
    assert ct.detect_language("— 3 — 12.5% ≥ 3") == "unknown"
    assert ct.clean_markdown(KEY_TERMS_ZH)["language"] == "zh"
    assert ct.clean_markdown(KEY_TERMS_EN)["language"] == "en"


# ---------------------------------------------------------------- registry

FACTS = {
    "completion_date": "2027-12",
    "status": "TERMINATED",
    "zz_extra": {"b": 2, "a": "不"},
    "phase": ["PHASE2", "PHASE3"],
    "why_stopped": "未见疗效，申办方撤回",
    "eligibility": {"eligibilityCriteria": "年龄 ≥18 岁；ALT 不超过 3×ULN；无显著心脏病史", "sex": "ALL"},
    "enrollment": 120,
    "trial_id": "NCT01234567",
    "has_results": False,
    "interventions": [{"name": "Drug X", "description": "10 mg/kg IV q3w"}],
}
LOCATORS = {k: {"json_pointer": "/p/" + k} for k in FACTS if k != "zz_extra"}


def test_registry_order_format_and_pointers():
    out = ct.clean_registry(FACTS, LOCATORS, summary="Corrected: no significant difference (p = 0.21).")
    assert_shape(out)
    segs = out["segments"]
    assert segs[0]["kind"] == "paragraph"
    assert segs[0]["source"] == {"json_pointer": "/protocolSection/descriptionModule/briefSummary"}
    assert seg_text(out, segs[0]) == "Corrected: no significant difference (p = 0.21)."
    keys = [seg_text(out, s).split(" ", 1)[1].split(":", 1)[0] for s in segs[1:]]
    assert keys == ["status", "why_stopped", "phase", "enrollment", "completion_date", "interventions",
                    "eligibility", "has_results", "trial_id", "zz_extra"]
    assert all(s["kind"] == "field" for s in segs[1:])
    lines = out["text"].split("\n")
    assert lines[1] == '状态 status: "TERMINATED"'
    assert lines[3] == '分期 phase: ["PHASE2","PHASE3"]'
    assert '"description":"10 mg/kg IV q3w","name":"Drug X"' in out["text"]
    assert lines[-1] == '其他字段 zz_extra: {"a":"不","b":2}'
    assert segs[1]["source"] == {"json_pointer": "/p/status"}
    assert segs[-1]["source"] == {"json_pointer": None}
    for term in ["未见疗效", "撤回", "≥18", "不超过", "无显著", "mg/kg", "no significant", "Corrected"]:
        assert term in out["text"], term
    assert out["warnings"] == [] and out["removed_lines"] == 0


def test_registry_missing_fields_not_invented_and_summary_missing():
    out = ct.clean_registry({"status": "RECRUITING", "why_stopped": None}, {})
    assert out["text"] == '状态 status: "RECRUITING"'
    assert "why_stopped" not in out["text"] and "null" not in out["text"]
    assert "enrollment" not in out["text"]
    assert out["warnings"] == ["summary_missing"]
    assert out["segments"][0]["source"] == {"json_pointer": None}


def test_registry_long_fields_are_kept_whole_for_search():
    """v2：入组标准等长字段完整保留（v1 的 4000 字截断在真实 CT 上截掉了 1796/4496 个版本的关键内容）。"""
    long = "未见异常；" * 1000
    out = ct.clean_registry({"eligibility": long}, {"eligibility": {"json_pointer": "/e"}}, summary="s")
    assert json.dumps(long, ensure_ascii=False) in out["text"] and out["warnings"] == []


def test_registry_long_value_cut_with_explicit_note():
    long = "未见异常；" * 50000
    out = ct.clean_registry({"eligibility": long}, {"eligibility": {"json_pointer": "/e"}}, summary="s")
    full = json.dumps(long, ensure_ascii=False)
    line = seg_text(out, out["segments"][1])
    assert line.endswith("…（共 %d 字符，见原件）" % len(full))
    assert line == "入选标准 eligibility: " + full[:ct.MAX_FIELD_CHARS] + "…（共 %d 字符，见原件）" % len(full)
    assert "field_truncated:eligibility" in out["warnings"]


def test_registry_empty():
    out = ct.clean_registry({}, {})
    assert out["text"] == "" and out["segments"] == []
    assert out["warnings"] == ["summary_missing", "no_body"]


# ---------------------------------------------------------------- literature

def lit(record):
    return json.dumps(record, ensure_ascii=False)


RECORD = {
    "source": "MED", "id": "123",
    "title": "Drug X in NSCLC: <i>no significant</i> benefit [Retracted]",
    "abstractText": ("<h4>BACKGROUND</h4>Patients received 10 mg/kg.<br/>ALT ≥3×ULN in 2.1%; "
                     "\"not\" inferior.\n\n<b>Corrected</b> 5 μg/mL, 约 30% 疑似无显著获益，未见死亡。"),
    "authorList": {"author": [{"title": "decoy"}]},
}


def test_literature_title_abstract_key_terms_and_tags():
    raw = lit(RECORD)
    out = ct.clean_literature(raw)
    assert_shape(out)
    title, abstract = out["segments"]
    assert title["kind"] == "title" and abstract["kind"] == "abstract"
    assert seg_text(out, title) == "Drug X in NSCLC: no significant benefit [Retracted]"
    assert seg_text(out, abstract) == ("BACKGROUND Patients received 10 mg/kg. ALT ≥3×ULN in 2.1%; "
                                       "\"not\" inferior.\nCorrected 5 μg/mL, "
                                       "约 30% 疑似无显著获益，未见死亡。")
    for term in ["no significant", "Retracted", "mg/kg", "≥3×ULN", "2.1%", "not", "Corrected", "μg/mL",
                 "约", "疑似", "无显著", "未见"]:
        assert term in out["text"], term
    assert out["text"] == seg_text(out, title) + "\n\n" + seg_text(out, abstract)
    assert title["source"]["json_pointer"] == "/title"
    assert abstract["source"]["json_pointer"] == "/abstractText"
    assert out["language"] == "en"
    assert out["warnings"] == []


@pytest.mark.parametrize("dump", [
    lambda r: json.dumps(r, ensure_ascii=False),
    lambda r: json.dumps(r, ensure_ascii=True, indent=2, sort_keys=True),
])
def test_literature_offsets_point_at_string_literal(dump):
    raw = dump(RECORD)
    out = ct.clean_literature(raw)
    for seg, key in zip(out["segments"], ["title", "abstractText"]):
        src = seg["source"]
        # source 是 JSON 字符串字面量去掉两侧引号后的内容：补回引号即可 json.loads 回原值
        assert raw[src["start"] - 1] == '"' and raw[src["end"]] == '"'
        assert json.loads(raw[src["start"] - 1:src["end"] + 1]) == RECORD[key]
        assert ct.normalize_whitespace(ct.strip_simple_html(RECORD[key])) == seg_text(out, seg)


def test_literature_sup_sub_flattening_is_flagged():
    out = ct.clean_literature(lit({"title": "IC<sub>50</sub> of 10<sup>-3</sup> M", "abstractText": "x"}))
    assert out["text"].startswith("IC50 of 10-3 M")
    assert "html_sup_sub_flattened" in out["warnings"]


def test_literature_no_abstract():
    out = ct.clean_literature(lit({"title": "【更正】 某药 不良反应", "abstractText": ""}))
    assert out["text"] == "【更正】 某药 不良反应"
    assert [s["kind"] for s in out["segments"]] == ["title"]
    assert out["warnings"] == ["no_abstract"]
    assert out["language"] == "zh"
    assert ct.clean_literature(lit({"title": "T"}))["warnings"] == ["no_abstract"]


def test_literature_no_body_and_bad_json():
    out = ct.clean_literature(lit({"id": "1"}))
    assert out["text"] == "" and out["warnings"] == ["no_abstract", "no_title", "no_body"]
    bad = ct.clean_literature("{not json")
    assert bad["text"] == "" and bad["segments"] == [] and bad["warnings"] == ["invalid_json", "no_body"]
    arr = ct.clean_literature("[1, 2]")
    assert arr["warnings"] == ["not_a_record", "no_body"]
