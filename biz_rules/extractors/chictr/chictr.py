"""中国临床试验注册中心（ChiCTR）详情页特解"""
import re


# 字段名到内部 key 的映射（中文标签 → key）
_FIELD_MAP = {
    "注册号":           "regno",
    "注册时间":         "publish_date",
    "最近更新日期":     "last_updated",
    "注册号状态":       "reg_status",
    "注册题目":         "title",
    "研究课题的正式科学名称": "scientific_title",
    "研究课题代号":     "study_id",
    "研究类型":         "study_type",
    "研究阶段":         "study_stage",
    "研究设计":         "study_design",
    "研究目的":         "objective",
    "干预措施":         "intervention",
    "目标疾病":         "condition",
    "纳入标准":         "inclusion_criteria",
    "排除标准":         "exclusion_criteria",
    "样本量":           "sample_size",
    "招募状态":         "recruitment_status",
    "研究实施负责（组长）单位": "primary_sponsor",
    "申请人所在单位":   "applicant_institution",
    "研究负责人所在单位": "leader_institution",
    "研究负责人":       "study_leader",
    "申请注册联系人":   "applicant",
    "是否获伦理委员会批准": "ethics_approved",
    "伦理委员会批准日期": "ethics_date",
    "批准本研究的伦理委员会名称": "ethics_committee",
    "预计研究开始日期": "start_date",
    "预计研究完成日期": "end_date",
    "实际研究开始日期": "actual_start_date",
    "实际研究完成日期": "actual_end_date",
    "经费或物资来源":   "funding_source",
    "研究实施地点":     "study_sites",
}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def extract_chictr_detail(page) -> dict:
    result = {
        "title": "",
        "publish_date": "",
        "body_html": "",
        "body_text": "",
        "attachment_links": [],
        "extra": {},
    }

    # 标题：div[class*='proj'] 第一个，或 table 里注册题目字段
    proj_divs = page.css("div[class*='proj']")
    if proj_divs:
        result["title"] = _clean(proj_divs[0].get_all_text(strip=True))

    # 遍历所有 table，提取 left_title td → 值 td 的键值对
    fields = {}
    tables = page.css("table")
    for tbl in tables:
        rows = tbl.css("tr")
        for row in rows:
            tds = row.css("td")
            if len(tds) < 2:
                continue
            # 字段名：取 .cn p 的文本，或直接取 td 文本
            label_td = tds[0]
            cn_ps = label_td.css("p.cn")
            if cn_ps:
                label = _clean(cn_ps[0].get_all_text(strip=True)).rstrip("：:")
            else:
                label = _clean(label_td.get_all_text(strip=True)).rstrip("：:")

            value = _clean(tds[1].get_all_text(strip=True))
            if label and value:
                fields[label] = value

    # 用字段映射填充 extra
    extra = {}
    for cn_label, key in _FIELD_MAP.items():
        if cn_label in fields:
            extra[key] = fields[cn_label]

    # 注册号 → 用作标识
    regno = extra.get("regno", "")

    # 标题兜底
    if not result["title"]:
        result["title"] = extra.get("title", "")
    if not result["title"] and regno:
        result["title"] = regno

    # 日期：优先注册时间
    result["publish_date"] = extra.get("publish_date", "")
    if not result["publish_date"]:
        result["publish_date"] = extra.get("last_updated", "")

    result["extra"] = extra

    # 拼接 body_text（所有字段的可读文本）
    lines = []
    if regno:
        lines.append(f"注册号：{regno}")
    for cn_label, key in _FIELD_MAP.items():
        val = extra.get(key, "")
        if val and key not in ("regno",):
            lines.append(f"{cn_label}：{val}")
    # 补充未映射的字段
    mapped_labels = set(_FIELD_MAP.keys())
    for label, val in fields.items():
        if label not in mapped_labels and val:
            lines.append(f"{label}：{val}")

    result["body_text"] = "\n".join(lines)

    # body_html：取第一个 table 的 html
    if tables:
        result["body_html"] = tables[0].html_content

    return result
