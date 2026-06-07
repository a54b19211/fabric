from db.repository import upsert_doc_extra


def db_writer(doc_uuid: str, parsed_result: dict) -> None:
    """将 ClinicalTrials.gov 专属字段写入 doc_extra 表。"""
    # 从 parsed_result 中提取 ClinicalTrials 特有字段
    extra = {
        "nct_id": parsed_result.get("nct_id", ""),
        "study_type": parsed_result.get("study_type", ""),
        "phase": parsed_result.get("phase", []),
        "study_status": parsed_result.get("study_status", ""),
        "enrollment": parsed_result.get("enrollment", 0),
        "study_completion_date": parsed_result.get("study_completion_date", ""),
        "primary_completion_date": parsed_result.get("primary_completion_date", ""),
        "sponsor": parsed_result.get("sponsor", ""),
        "collaborators": parsed_result.get("collaborators", []),
        "conditions": parsed_result.get("conditions", []),
        "interventions": parsed_result.get("interventions", []),
        "arm_groups": parsed_result.get("arm_groups", []),
    }
    
    # 从extra_fields中获取额外的字段
    extra_fields = parsed_result.get("extra_fields", {})
    if extra_fields:
        extra.update({
            "status": extra_fields.get("status", ""),
            "phase": extra_fields.get("phase", []),
            "enrollment": extra_fields.get("enrollment", 0),
            "completion_date": extra_fields.get("completion_date", ""),
        })
    
    # 过滤空值
    extra = {k: v for k, v in extra.items() if v not in [None, "", [], 0]}
    
    if extra:
        upsert_doc_extra(doc_uuid, "clinicaltrials", extra)
        print(f"[ClinicalTrials] 写入 doc_extra: {doc_uuid} | 字段: {list(extra.keys())}")
    else:
        print(f"[ClinicalTrials] 无特有字段可写入 doc_extra: {doc_uuid}")