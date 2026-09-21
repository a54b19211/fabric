"""
ClinicalTrials.gov 站点配置
使用官方 API 获取临床试验数据
"""

from biz_rules.extractors.clinicaltrials_fetcher import fetch_clinicaltrials
from biz_rules.cleaners.clinical_trial import clean_clinical_trial

SITE_KEY = "clinicaltrials"

CONFIG = {
    "name": "ClinicalTrials.gov 临床试验库",
    "source_type": "api",                    # 标记为 API 类型
    "api_fetcher": fetch_clinicaltrials,     # 数据拉取函数

    # API 请求配置（传给 fetcher）
    "api": {
        "base_url": "https://clinicaltrials.gov/api/v2/studies",
        "params": {
            "query.term": "cancer",          # 搜索条件（可按需修改）
            "pageSize": 20,                  # 每页数量
            "format": "json",
        },
        "max_pages": 10,                     # 最大翻页数
    },

    # 字段映射：API 原始数据 → 标准字段
    "uid_field": "protocolSection.identificationModule.nctId",  # 唯一标识

    "field_map": {
        "title": "protocolSection.identificationModule.briefTitle",
        "publish_date": "protocolSection.statusModule.lastUpdatePostDateStruct.date",
        "summary": "protocolSection.descriptionModule.briefSummary",
        "drug_name": "drug_names",
        "manufacturer": "",
        "category": "__const__:临床试验",
    },

    # 来源特有字段：与 biz_rules/schemas.py 的 ClinicalTrialsExtra 字段一一对应
    "extra_fields": {
        "nct_id": "protocolSection.identificationModule.nctId",
        "study_type": "protocolSection.designModule.studyType",
        "phase": {"path": "protocolSection.designModule.phases", "type": "list"},
        "study_status": "protocolSection.statusModule.overallStatus",
        "enrollment": {"path": "protocolSection.designModule.enrollmentInfo.count", "type": "int"},
        "study_completion_date": "protocolSection.statusModule.completionDateStruct.date",
        "primary_completion_date": "protocolSection.statusModule.primaryCompletionDateStruct.date",
        "sponsor": "protocolSection.sponsorCollaboratorsModule.leadSponsor.name",
        "collaborators": {"path": "protocolSection.sponsorCollaboratorsModule.collaborators", "type": "list", "key": "name"},
        "conditions": {"path": "protocolSection.conditionsModule.conditions", "type": "list"},
        "interventions": {"path": "protocolSection.armsInterventionsModule.interventions", "type": "list", "key": "name"},
        "arm_groups": {"path": "protocolSection.armsInterventionsModule.armGroups", "type": "list", "key": "label"},
    },

    # 爬取控制
    "request_delay_seconds": 0.5,
    "timeout": 30,
    "content_cleaner": clean_clinical_trial,
}