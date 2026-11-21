"""小标题规则（clean/headings.py）和第二轮审计的页面、附件格式问题，用审计里的原句。"""
from scrapling import Selector

from clean import headings
from clean.pdf_text import join_pages


def test_numbered_short_lines_questions_and_separators_are_headings_body_lines_are_not():
    assert headings.rank("一、【成份】项") == "cn"  # 复查 A-D2
    assert headings.rank("一、《公告》制定的背景和目的是什么？") == "cn"  # B-D2：问句标题
    assert headings.rank("问：《意见》的起草背景是什么？") == "q"
    assert headings.rank("十一、出口和接受委托生产出口麻醉药品、精神药品、含麻醉药品复方制剂的有关说明",
                         emphasized=True) == "cn"  # 加粗的长标题
    assert headings.rank("附件1 严重缺陷(举例)") == "sep"  # A-D7
    assert headings.rank("1.1 总体要求") == "d2" and headings.rank("6.2.1 响应分级") == "d3"  # B-D8
    assert headings.rank("第一条（目的和依据）") == "tiao"
    for body in ("第四条（概念界定） 市场独占期，是指国家药监局依申请对符合条件的药品实施保护。",
                 "同品种药品，是指通用名称、活性成分/中药处方、剂型、适应症均相同的药品",  # B-D5：大纲级别的正文
                 "1.有证据表明，该药物涉及的适应症发病率或患病率较低；", "附件：1.放射性药品管理条例（征求意见稿）",
                 "2021 年 5 月 8 日", "1.1 编写目的 ............ 3", "1.《儿童抗肿瘤药物临床研发技术指导原则》."):
        assert headings.rank(body, styled=True) is None, body
    assert headings.split_article("第四条（概念界定） 市场独占期，是指……") == ("第四条（概念界定）", "市场独占期，是指……")


def test_levels_follow_the_numbering_pattern_not_the_order_of_appearance():
    text = "# 标题\n\n### （一）甲\n\n正文。\n\n## 一、乙\n\n正文。\n\n# 第一章 总则\n\n正文。"
    assert headings.relevel(text).split("\n\n") == ["# 标题", "#### （一）甲", "正文。", "### 一、乙", "正文。",
                                                   "## 第一章 总则", "正文。"]


def test_attachment_listing_lines_and_short_separators_are_not_headings():
    text = "正文。\n\n附件：1.某办法\n\n2.意见建议反馈表\n\n附件1\n\n" + "条文。" * 80 + "\n\n附件2\n\n短。"
    out = headings.relevel(text, promote=True).split("\n\n")
    assert out[1:4] == ["附件：1.某办法", "2.意见建议反馈表", "## 附件1"] and out[-2:] == ["附件2", "短。"]


def test_private_use_characters_are_removed_or_shown_as_symbols():
    assert headings.strip_private_use("第一章 范 围\n 申请 通过\U000f0001") == "第一章 范 围\n□ 申请☑ 通过"
    assert headings.escape_markdown("在“**ml复方樟脑酊”后") == "在“\\*\\*ml复方樟脑酊”后"  # A-D8


def test_nmpa_page_bold_question_cell_paragraphs_and_colspan():
    from fetch.extractors.nmpa import extract_nmpa_detail

    html = ("<html><head><meta name='ArticleTitle' content='解读'></head><body><div class='wenzhang'>"
            "<div class='wenzhang-table'><table><tr><td>主题分类</td><td>法规文件 / 规范性文件</td></tr></table></div>"
            "<div class='text'><p><strong>问：《意见》的起草背景是什么？</strong></p><p>答：为了……。</p>"
            "<p>具体流程参见下图：</p><p><img src='/directory/web/nmpa/images/1.png'></p>"
            "<table><tr><td>检查单位</td><td><p>国家食品药品监督管理总局核查中心</p><p>镇江市食品药品监督管理局</p></td>"
            "</tr><tr><td colspan='2'>一、工艺验证。</td></tr></table></div></div></body></html>")
    got = extract_nmpa_detail(Selector(html))
    blocks = got["body_text"].split("\n\n")
    assert blocks[0] == "## 问：《意见》的起草背景是什么？"  # B-D2：加粗问句
    assert "| 检查单位 | 国家食品药品监督管理总局核查中心<br>镇江市食品药品监督管理局 |" in blocks[-1]  # C-P1
    assert "| 一、工艺验证。 |  |" in blocks[-1]  # colspan：值只写一次
    assert got["extra"]["content_in_media"] == "partial" and got["extra"]["subject_category"] == "法规文件 / 规范性文件"


def test_pdf_paragraph_broken_at_a_page_end_is_joined():
    pages = ["技术问题（包括但不限于：", "研发策略、研究设计等）的相应指导。\n\n下一段。", "## 二、主要内容"]
    pages[0] = "第一段。\n\n获得其抗肿瘤药物在儿童人群研究过程中" + pages[0]
    assert join_pages(pages)[0].endswith("技术问题（包括但不限于：研发策略、研究设计等）的相应指导。")
    assert join_pages(["这一页最后一段还没有写完的一句话没有句号", "| 表 |"])[1] == "| 表 |"


# ── 第三轮审计（复查3-页面 D1–D6、复查3-附件 D1–D8、N1–N6）──


def test_round3_heading_rules_field_lines_lead_ins_notices_and_centered_titles():
    assert headings.rank("（三）联系方式：马 静 010-63316018") is None  # 页面 D2
    assert headings.rank("请仔细阅读说明书并按说明使用或在药师指导下购买和使用", emphasized=True) is None  # 附件 D7
    assert headings.rank("暖宫七味散说明书", emphasized=True) == "u"
    assert headings.rank("严重缺陷(举例)", centered=True) == "u" and headings.rank("严重缺陷(举例)") is None  # 页面 D4
    assert headings.rank("曹 亢 010-63368326", centered=True) is None
    lead_in = "# 标题\n\n（一）药品委托分段生产是持有人委托的行为。\n\n#### （二）委托分段生产药品应当属于下列品种之一：" \
              "\n\n1.甲；\n\n（三）注册申请人应提出申请。"
    assert "（二）委托分段生产药品应当属于下列品种之一：" in headings.relevel(lead_in).split("\n\n")  # 附件 D7
    sections = "# 标题\n\n## 一、警示语应包含：\n\n正文。\n\n## 二、【不良反应】修订为：\n\n正文。"
    assert headings.relevel(sections).split("\n\n")[1] == "## 一、警示语应包含："  # 同级都是标题：照旧
    listing = "# 标题\n\n附：1.罕见病药品注册分类与市场独占期\n\n##### 2.儿童药注册分类与市场独占期\n\n## 附1\n\n" + "正文。" * 80
    assert "2.儿童药注册分类与市场独占期" in headings.relevel(listing).split("\n\n")  # 附件 D4


def test_round3_stars_scripts_and_arrow_bullet():
    assert headings.escape_stars("仅填写“*”项") == "仅填写“*”项"  # N2：单个星号不会成对
    assert headings.escape_stars("在“**ml复方樟脑酊”后") == "在“\\*\\*ml复方樟脑酊”后"
    assert headings.escape_stars("| 头痛* | 常见 | \\*\\* |") == "| 头痛* | 常见 | \\*\\* |"
    assert headings.scripted("2", "sub") == "₂" and headings.scripted("2+", "sup") == "²⁺"  # N4
    assert headings.scripted("±0.5", "sup") == "^(±0.5)"
    assert headings.strip_private_use("建立参照人群") == "➢ 建立参照人群"  # 附件 D2：➢ 不写成 ◆


def test_round3_tables_form_rows_stay_and_headerless_tables_get_column_names():
    from clean.excel_parser import SPAN, markdown_table

    form = markdown_table([["申请编号", "", SPAN], ["产品名称", "中文名称", "甲"]])
    assert form.splitlines() == ["| 列1 | 列2 | 列3 |", "| --- | --- | --- |", "| 申请编号 |  |  |",
                                 "| 产品名称 | 中文名称 | 甲 |"]  # 附件 D8/D5：标签行留在表里，字段行不当表头
    assert markdown_table([["名单", SPAN, SPAN], ["序号", "名称", "备注"], ["1", "甲", ""]]).startswith("名单\n\n| 序号")
    labelled = markdown_table([["已发布的产品信息", SPAN, SPAN, "对应关系"], ["序号", "产品名称", "产品描述", "备注"],
                               ["1", "甲", "乙", "/"]])
    assert labelled.splitlines()[:3] == ["| 序号 | 产品名称 | 产品描述 | 备注 |", "| --- | --- | --- | --- |",
                                         "| 已发布的产品信息 |  |  | 对应关系 |"]  # 栏目名行当表头，上层分组行放在下面
    merged = markdown_table([["目录", SPAN, SPAN, SPAN, "新目录"], ["序号", "品名举例", SPAN, "对应位置", "备注"],
                             ["1", "刀", "刀", "02-01", "/"]])
    assert merged.splitlines()[0] == "| 序号 | 品名举例 |  | 对应位置 | 备注 |"  # 栏目名行里有横跨两列的格


def test_round3_word_cell_soft_breaks_and_subscripts(tmp_path):
    import zipfile

    from clean import office_text

    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    cell = "<w:tc><w:p><w:r><w:t>应当确保：</w:t><w:br/><w:t>（一）甲；</w:t><w:br/><w:t>（二）乙。</w:t></w:r></w:p></w:tc>"
    sub = '<w:p><w:r><w:t>β</w:t></w:r><w:r><w:rPr><w:vertAlign w:val="subscript"/></w:rPr><w:t>2</w:t></w:r>' \
          '<w:r><w:t>受体激动剂</w:t></w:r></w:p>'
    path = tmp_path / "a.docx"
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("word/document.xml", f"<w:document {w}><w:body>{sub}<w:tbl><w:tr><w:tc><w:p><w:r><w:t>条款"
                                              f"</w:t></w:r></w:p></w:tc>{cell}</w:tr></w:tbl></w:body></w:document>")
    markdown = office_text.parse_docx(str(path))["markdown"]
    assert "β₂受体激动剂" in markdown and "应当确保：<br>（一）甲；<br>（二）乙。" in markdown  # N4、附件 D6


def test_round3_attachment_title_block_label_noise_and_missing_glyph_warning():
    from clean import clean_stage

    body = "## 附件3\n\n增设允许药材进口口岸工作评估考核\n\n实施方案\n\n### 一、工作目的\n\n正文。"
    assert clean_stage.attachment_name("药监综药注〔2026〕36号附件3.doc", body) == "附件3 增设允许药材进口口岸工作评估考核实施方案"
    assert clean_stage.attachment_name("附件1.docx", "放射性药品管理条例\n\n（征求意见稿）\n\n第一章 总 则") == \
        "附件1 放射性药品管理条例（征求意见稿）"  # 附件 D3
    assert clean_stage.attachment_name("国家药品监督管理局2024年第59号公告附件4.docx", "## 附件4\n\n申请编号：（自动带入）\n\n"
                                       "医疗器械分类界定申请表（格式）\n\n产品名称：") == "附件4 医疗器械分类界定申请表（格式）"
    assert clean_stage.attachment_name("国家药品监督管理局2024年第59号公告附件4.docx", "| a | b |") == \
        "国家药品监督管理局2024年第59号公告附件4"  # 没有题名：整个链接文字，不只是“附件4”
    assert clean_stage.attachment_name("附件2 ：第一批试点医疗器械企业.docx", "") == "附件2 第一批试点医疗器械企业"  # N6
    out = clean_stage._clean_by_basis("nmpa", {"title": "a.pdf", "facts": {}, "text": "正文。", "method": "pypdf_text",
                                               "attempts": ["pypdf_text:ok", "pypdf_text:missing_glyphs:1,2"]},
                                      "attachment_with_parent")
    assert "attachment_missing_glyphs:1,2" in out["warnings"]  # 附件 D1


def test_round3_pdf_line_joins():
    from clean import pdf_text

    assert pdf_text._open_line("技术问题（包括但不限于：", 500, 500, 16)[0]  # 括号没闭合的“：”不是段落结束
    lines = [(700, 90, 500, "目标疗效", 16), (680, 90, 500, "评估外推可靠性。", 16)]
    assert len(pdf_text._paragraphs(lines * 1)) == 2  # 下一行以项目符号开头：不接
    listing = [(700, 127, 538, "附件：1.儿童抗肿瘤药物研发鼓励试点计划（星光计划）", 16), (680, 196, 261, "申报指南", 16)]
    assert pdf_text._paragraphs(listing) == ["附件：1.儿童抗肿瘤药物研发鼓励试点计划（星光计划）申报指南"]
    assert pdf_text.join_cjk("其他国家", "/地区") == "其他国家/地区"
    assert not pdf_text._LIST_START.match("第三章）和/或数据分析")


def test_round3_nmpa_page_line_bold_centered_titles_source_note_and_media():
    from fetch.extractors.nmpa import extract_nmpa_detail

    page = ("<html><head><meta name='ArticleTitle' content='通知'></head><body><div class='wenzhang'>"
            "<div class='date'>文章来源：四川省药品监督管理局网站 发布时间：2021-05-10</div><div class='text'>"
            "<p style='text-align:right'>2026年9月20日</p><p><br>　<br><strong>\xa0\xa0优化中药补充申请审评审批程序改革"
            "工作方案</strong><br>　　为贯彻……，制定本方案。</p>"
            "<p style='text-align:center'>疫苗生产场地变更质量可比性<br>研究技术指导原则</p>"
            "<p>附件1<br>" + "　" * 16 + " 严重缺陷(举例)</p><p>（三）联系方式：马 静 010-63316018</p>"
            "<table><tr><td>处理措施</td><td>撤销证书<img src='/x/1715674733267076102.png'>，立案调查。</td></tr></table>"
            "<p>特此通告。</p><p>附件：</p></div></div></body></html>")
    got = extract_nmpa_detail(Selector(page))
    blocks = got["body_text"].split("\n\n")
    assert "## 优化中药补充申请审评审批程序改革工作方案" in blocks  # 页面 D5：同段里整行加粗的一行
    assert "## 疫苗生产场地变更质量可比性研究技术指导原则" in blocks and "## 严重缺陷(举例)" in blocks  # 页面 D4
    assert "（三）联系方式：马 静 010-63316018" in blocks  # 页面 D2
    assert blocks[-1] == "特此通告。" and not any("文章来源" in b for b in blocks)  # 页面 D6
    assert got["extra"]["source_note"] == "四川省药品监督管理局网站"
    assert got["extra"]["media"]["images"] == ["https://www.nmpa.gov.cn/x/1715674733267076102.png"]  # 页面 D3
    assert got["extra"]["content_in_media"] == "partial"
