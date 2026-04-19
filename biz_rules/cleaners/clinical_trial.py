"""
临床试验数据清洗器（chictr / clinicaltrials）。

问题：结构化字段直接转成 key:value 格式，图谱提取效果差。
策略：将关键字段重组为叙述性自然语言段落，保留原始字段作为附录。
"""

import re


def clean_clinical_trial(parsed_result: dict) -> dict:
    """
    将临床试验的结构化 markdown 重组为叙述性文本。
    适用于 chictr 和 clinicaltrials 两个站点。
    """
    md = parsed_result.get("markdown", "")
    title = parsed_result.get("title", "")
    structured = parsed_result.get("structured_data", {}) or {}
    site = parsed_result.get("source_site", "")

    if site == "chictr":
        parsed_result["markdown"] = _rewrite_chictr(md, title, structured)
    elif site == "clinicaltrials":
        parsed_result["markdown"] = _rewrite_clinicaltrials(md, title, structured)

    return parsed_result


def _extract_field(md: str, field_name: str) -> str:
    """从 markdown 文本中提取指定字段的值（支持中英文标签）。"""
    pattern = rf"(?:^|\n){re.escape(field_name)}[：:]\s*(.+?)(?:\n|$)"
    m = re.search(pattern, md, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _rewrite_chictr(md: str, title: str, structured: dict) -> str:
    """ChiCTR 数据重写为叙述性段落。"""
    regno = _extract_field(md, "注册号") or _extract_field(md, "Registration number")
    reg_time = _extract_field(md, "注册时间")
    study_type = _extract_field(md, "研究类型") or _extract_field(md, "Study type")
    study_design = _extract_field(md, "研究设计") or _extract_field(md, "Study design")
    objective = _extract_field(md, "研究目的") or _extract_field(md, "Objectives of Study")
    inclusion = _extract_field(md, "纳入标准") or _extract_field(md, "Inclusion criteria")
    exclusion = _extract_field(md, "排除标准") or _extract_field(md, "Exclusion criteria")
    sponsor = _extract_field(md, "研究实施负责（组长）单位") or _extract_field(md, "Primary sponsor")
    disease = _extract_field(md, "研究疾病") or _extract_field(md, "Target disease")
    start_date = _extract_field(md, "研究实施时间")
    recruitment = _extract_field(md, "征募研究对象情况") or _extract_field(md, "Recruitment Status")

    paragraphs = [f"# {title}", ""]

    # 基本信息段
    basic_parts = []
    if regno:
        basic_parts.append(f"注册号 {regno}")
    if reg_time:
        basic_parts.append(f"注册于 {reg_time}")
    if study_type:
        basic_parts.append(f"研究类型为{study_type}")
    if study_design:
        basic_parts.append(f"采用{study_design}设计")
    if basic_parts:
        paragraphs.append("，".join(basic_parts) + "。")

    if disease:
        paragraphs.append(f"研究针对疾病：{disease}。")
    if sponsor:
        paragraphs.append(f"主办单位：{sponsor}。")
    if start_date:
        paragraphs.append(f"研究实施时间：{start_date}。")
    if recruitment:
        paragraphs.append(f"招募状态：{recruitment}。")

    if objective:
        paragraphs += ["", "## 研究目的", "", objective]

    if inclusion:
        paragraphs += ["", "## 纳入标准", "", inclusion]

    if exclusion:
        paragraphs += ["", "## 排除标准", "", exclusion]

    # 附录：保留原始完整内容供全文检索
    paragraphs += ["", "---", "", "## 原始注册信息", "", md]

    return "\n".join(paragraphs)


def _rewrite_clinicaltrials(md: str, title: str, structured: dict) -> str:
    """ClinicalTrials.gov 数据重写为叙述性段落。"""
    summary = structured.get("summary", "") or _extract_field(md, "summary")
    drug_name = structured.get("drug_name", "")
    status = _extract_field(md, "status") or _extract_field(md, "overallStatus")
    phase = _extract_field(md, "phase")
    enrollment = _extract_field(md, "enrollment")
    start_date = structured.get("publish_date", "")

    paragraphs = [f"# {title}", ""]

    basic_parts = []
    if status:
        basic_parts.append(f"当前状态：{status}")
    if phase:
        basic_parts.append(f"研究阶段：{phase}")
    if enrollment:
        basic_parts.append(f"计划入组人数：{enrollment}")
    if start_date:
        basic_parts.append(f"开始日期：{start_date}")
    if basic_parts:
        paragraphs.append("，".join(basic_parts) + "。")

    if drug_name:
        paragraphs.append(f"研究涉及药物/干预：{drug_name}。")

    if summary:
        paragraphs += ["", "## 研究摘要", "", summary]

    paragraphs += ["", "---", "", "## 原始数据", "", md]

    return "\n".join(paragraphs)
