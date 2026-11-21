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
