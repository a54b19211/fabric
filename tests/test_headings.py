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
    # 格内两段连成一行（切块审计：表格里不留 <br>）；整行一格的行不进表格，是正文
    assert blocks[-2] == "| 检查单位 | 国家食品药品监督管理总局核查中心；镇江市食品药品监督管理局 |\n| --- | --- |"
    assert blocks[-1] == "一、工艺验证。"
    assert got["extra"]["content_in_media"] == "partial" and got["extra"]["subject_category"] == "法规文件 / 规范性文件"


def test_nmpa_inspection_report_full_width_rows_become_sections():
    """飞行检查通报：“检查发现问题”整行一格是小标题，下一行整行一格的正文按段落写，不塞进表格（切块审计 FAIL）。"""
    from fetch.extractors.nmpa import extract_nmpa_detail

    html = ("<html><head><meta name='ArticleTitle' content='飞检'></head><body><div class='wenzhang'><div class='text'>"
            "<table><tr><td>企业名称</td><td>甲公司</td><td>法定代表人</td><td>乙</td></tr>"
            "<tr><td>注册地址</td><td colspan='3'>某市某路1号</td></tr>"
            "<tr><td colspan='4'>检查发现问题</td></tr>"
            "<tr><td colspan='4'><p>1.企业擅自改变注册地址。</p><p>2.计算机系统不符合要求。</p></td></tr>"
            "<tr><td colspan='4'>处理措施</td></tr>"
            "<tr><td colspan='4'>省局已依法撤销该企业认证证书，对企业涉嫌违法违规经营行为立案调查。</td></tr>"
            "</table></div></div></body></html>")
    blocks = extract_nmpa_detail(Selector(html))["body_text"].split("\n\n")
    assert blocks[0].startswith("| 企业名称 | 甲公司 | 法定代表人 | 乙 |")
    assert "| 注册地址 | 某市某路1号 |  |  |" in blocks[0]
    assert blocks[1:] == ["## 检查发现问题", "1.企业擅自改变注册地址。", "2.计算机系统不符合要求。", "## 处理措施",
                          "省局已依法撤销该企业认证证书，对企业涉嫌违法违规经营行为立案调查。"]


def test_table_cells_header_span_group_notes_and_bare_attachment_label():
    from clean.excel_parser import SPAN, flat_cell, markdown_table

    assert flat_cell("核查中心<br>湖北省局") == "核查中心；湖北省局"
    assert flat_cell("社会信用代码<br>（组织机构代码）") == "社会信用代码（组织机构代码）"
    assert flat_cell("1.1 核实。<br>1.2 审核。") == "1.1 核实。1.2 审核。"
    # “序号”横跨两列，前一列是大类名、后一列是序号：表头名挪到序号那一列（切块审计 6ea7bcfb）
    got = markdown_table([["序号", SPAN, "标准内容"], ["一、战略规划", "1", "甲"], ["一、战略规划", "2", "乙"]])
    assert got.split("\n")[0] == "|  | 序号 | 标准内容 |"
    # 分组行里的长编辑备注写在表格前面，分组名进表头（切块审计 062004cc）
    rows = [["产品信息", SPAN, "对应关系", "1.本列整体去掉<br>2.只整理确定的产品。"],
            ["序号", "产品名称", "新目录位置", "包类"], ["1", "支具", "19-03", "包含"]]
    lead, table = markdown_table(rows).split("\n\n")
    assert lead == "1.本列整体去掉；2.只整理确定的产品。"
    assert table.split("\n")[0] == "| 产品信息／序号 | 产品名称 | 对应关系／新目录位置 | 包类 |"
    # 光秃秃的“附件2”下面一行短字是附件名，并进标题；带冒号的表单栏不并（切块审计 c39a2aad）
    body = "正文。" * 120
    got = headings.relevel("## 附件2\n\n儿童抗肿瘤药物研发实施框架\n\n" + body)
    assert got.startswith("## 附件2 儿童抗肿瘤药物研发实施框架\n\n")
    got = headings.relevel("## 附件4\n\n申请编号：（自动带入）\n\n" + body)
    assert got.startswith("## 附件4\n\n申请编号：（自动带入）\n\n")


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
    assert form.splitlines() == ["| 申请编号 |  |  |", "| --- | --- | --- |",
                                 "| 产品名称 | 中文名称 | 甲 |"]  # 附件 D8：标签行留在表里；第四轮 N2：不补“列1”
    assert markdown_table([["名单", SPAN, SPAN], ["序号", "名称", "备注"], ["1", "甲", ""]]).startswith("名单\n\n| 序号")
    labelled = markdown_table([["已发布的产品信息", SPAN, SPAN, "对应关系"], ["序号", "产品名称", "产品描述", "备注"],
                               ["1", "甲", "乙", "/"]])
    assert labelled.splitlines()[:2] == ["| 已发布的产品信息／序号 | 产品名称 | 产品描述 | 对应关系／备注 |",
                                         "| --- | --- | --- | --- |"]
    # 栏目名行当表头，上面的分组行写进表头（第五轮 E3，第四轮 D-D 是写在表前）
    merged = markdown_table([["目录", SPAN, SPAN, SPAN, "新目录"], ["序号", "品名举例", SPAN, "对应位置", "备注"],
                             ["1", "刀", "刀", "02-01", "/"]])
    assert merged.splitlines()[0] == "| 目录／序号 | 品名举例 |  | 对应位置 | 新目录／备注 |"  # 栏目名行里有横跨两列的格


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
    assert "β₂受体激动剂" in markdown and "应当确保：（一）甲；（二）乙。" in markdown  # N4、附件 D6


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


def test_round4_question_with_full_stop_centered_title_above_separators_and_toc_leaders():
    from clean import headings

    q = "二、《规定》要求，批件持有人在批件有效期内完成研究，提出药品注册申请。“药品注册申请”如何理解？"
    assert headings.rank(q) == "cn"  # N1：以“？”结尾的编号问句前面带句号也是标题
    assert headings.rank("二、提出药品注册申请。请按要求办理。") is None
    body = "正文" * 120
    md = headings.relevel("\n\n".join(["# 通知", "## 风险评定指导原则", "### 一、缺陷的分类", body, "## 附件1",
                                         "## 严重缺陷(举例)", "### 一、厂房", body]))
    assert "## 风险评定指导原则" in md and "## 附件1 严重缺陷(举例)" in md and "### 一、厂房" in md  # N3
    assert "### 一、缺陷的分类" in md
    toc = headings.relevel("目录\n\n1、有效性外推（如适用）....................9\n\n正文")
    assert "1、有效性外推（如适用）\n\n正文" in toc  # D-B：目录点线和页码去掉


def test_round4_word_centered_title_and_subscript_from_character_style(tmp_path):
    import zipfile

    from clean import office_text

    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    styles = (f'<w:styles {w}><w:style w:type="character" w:styleId="Font101"><w:rPr><w:vertAlign w:val="subscript"/>'
              '</w:rPr></w:style><w:style w:type="paragraph" w:styleId="Mid"><w:pPr><w:jc w:val="center"/></w:pPr>'
              '</w:style></w:styles>')
    centered = '<w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:t>参加改革工作</w:t></w:r></w:p>' \
               '<w:p><w:pPr><w:pStyle w:val="Mid"/></w:pPr><w:r><w:t>申报资料要求</w:t></w:r></w:p>'
    sub = '<w:p><w:r><w:t>按C</w:t></w:r><w:r><w:rPr><w:rStyle w:val="Font101"/></w:rPr><w:t>13</w:t></w:r>' \
          '<w:r><w:t>H计。</w:t></w:r></w:p>'
    path = tmp_path / "a.docx"
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("word/document.xml", f"<w:document {w}><w:body>{centered}{sub}</w:body></w:document>")
        package.writestr("word/styles.xml", styles)
    markdown = office_text.parse_docx(str(path))["markdown"]
    assert markdown.startswith("## 参加改革工作申报资料要求\n\n")  # S1：居中（含样式里的居中）的题名，两行接成一个
    assert "按C₁₃H计。" in markdown  # D-C：字符样式里的下标


def test_round4_pdf_reference_wrap_and_table_cell_across_pages():
    from clean import pdf_text

    assert pdf_text._reference_wrap("2.US FDA Guidance for Industry: Pediatric Study Plans:", "Content of and Process")
    assert not pdf_text._reference_wrap("1.《儿童抗肿瘤药物临床研发技术指导原则》.", "2.US FDA")
    page = "| 项目 | 说明 |\n| --- | --- |\n| 非临床研究计划 | 简要概述拟定的非临床研究计划和/或现有数据 |"
    out = pdf_text.join_pages([page, "如何支持儿童研究和使用。\n\n（表格中斜体内容提交时删除）"])
    assert out[0].endswith("| 非临床研究计划 | 简要概述拟定的非临床研究计划和/或现有数据如何支持儿童研究和使用。 |")
    assert out[1] == "（表格中斜体内容提交时删除）"
    main = {"x": 90, "y": 405.24, "size": 16.08, "text": "如 C"}
    assert pdf_text._script_place({"x": 121, "y": 403.8, "size": 10.56, "text": "max"}, main, 122.0) == "sub"


def test_round5_hidden_title_bullets_attachment_label_and_group_rows_in_header():
    from clean import pdf_text
    from clean.excel_parser import SPAN, markdown_table
    from fetch.extractors.nmpa import extract_nmpa_detail

    page = ("<html><body><div class='wenzhang'><div class='text'><meta name='GENERATOR' content='FrontPage'>"
            "<title>新建网页 1</title><p style='display: none'>看不见</p><p hidden>也看不见</p>"
            "<p>正文第一段。</p></div></div></body></html>")
    body = extract_nmpa_detail(Selector(page))["body_text"]
    assert body == "正文第一段。"  # N4：<title>、display:none、hidden 的字不进正文
    lines = [(700, 90, 100, "●", 16), (700, 110, 500, "研究目的：说明纳入主要和次要目的/终点的 PK 参数和", 16),
             (680, 110, 300, "/或 PD 生物标志物；", 16), (660, 90, 100, "■", 16), (660, 110, 300, "研究编号；", 16)]
    assert pdf_text._paragraphs(lines) == ["●研究目的：说明纳入主要和次要目的/终点的 PK 参数和/或 PD 生物标志物；",
                                           "■研究编号；"]  # E1：● ■ 和 • 一样，单独一个符号的行接下一行
    body_text = "正文" * 120
    title = "国家药品监督管理局医疗器械标准管理中心医疗器械产品分类界定申请告知书（格式）"
    md = headings.relevel("\n\n".join(["# 附件5", "## 附件5", "### " + title, body_text]))
    assert "## 附件5 " + title in md  # E2：标签加题名超过 40 字也并
    md = headings.relevel("\n\n".join(["# 附件4", "## 附件4", "申请编号：（自动带入）", "### 申请表（格式）", body_text]))
    assert "## 附件4 申请表（格式）\n\n申请编号：（自动带入）" in md  # E2：中间隔一行短字
    table = markdown_table([["第一类目录", SPAN, "新目录"], ["6801 基础外科", SPAN, "备注"],
                            ["序号", "产品类别", "备注"], ["1", "刀", "/"]])
    assert table.splitlines()[0] == "| 第一类目录／6801 基础外科／序号 | 产品类别 | 新目录／备注 |"  # E3


def test_final6_bullet_hanging_lines_and_second_subtable_inside_one_table():
    from clean import pdf_text
    from clean.excel_parser import SPAN, markdown_table

    lines = [(700, 90, 510, "●请阐明样本量的计算方式，以确定受试者人数。", 16.08),
             (680, 117.9, 505, "样本量应支持研究目的。", 16.08),
             (660, 122, 508, "如适用，对于不属于上述研究类型的研究。", 16.08)]
    assert pdf_text._paragraphs(lines) == ["●请阐明样本量的计算方式，以确定受试者人数。样本量应支持研究目的。",
                                           "如适用，对于不属于上述研究类型的研究。"]  # D1：续行接回，首行缩进两字的新段不接
    rows = [["第一类目录", SPAN, "新目录"], ["6822 光学", SPAN, "备注"], ["序号", "产品类别", "备注"], ["1", "镜", "/"],
            ["第一类目录", SPAN, "新目录"], ["6823 超声", SPAN, "备注"], ["序号", "产品类别", "备注"], ["1", "耦合剂", "/"]]
    out = markdown_table(rows)
    assert "| 第一类目录／6823 超声／序号 | 产品类别 | 新目录／备注 |" in out  # D2：表中间的第二张分表另起表头
    assert out.count("\n| --- |") == 2
