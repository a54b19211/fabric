"""
ClinicalTrials.gov 站点配置
使用官方 API 获取临床试验数据
"""

SITE_KEY = "clinicaltrials"

CONFIG = {
    "name": "ClinicalTrials.gov 临床试验库",
    "url_pattern": "clinicaltrials.gov",
    "type": "api",  # 标记为 API 类型，区别于普通网页

    # API 配置
    "api_config": {
        "base_url": "https://clinicaltrials.gov/api/v2/studies",
        "method": "GET",
        "params": {
            "format": "json"
            # pageSize 不在这里，改到每个 query 中
        },
        "pagination": {
            "type": "token",
            "param_name": "pageToken",
            "response_field": "nextPageToken"
        }
    },

    # 搜索条件（可配置多个）
    "queries": [
        {"query.term": "cancer", "pageSize": 10},
        {"query.term": "diabetes", "pageSize": 10},
    ],

    # 字段映射
    "field_mapping": {
        "uuid": "protocolSection.identificationModule.nctId",
        "title": "protocolSection.identificationModule.briefTitle",
        "status": "protocolSection.statusModule.overallStatus",
        "phase": "protocolSection.designModule.phaseList",
        "conditions": "protocolSection.conditionsModule.conditions",
        "enrollment": "protocolSection.designModule.enrollmentInfo.count",
        "start_date": "protocolSection.statusModule.startDateStruct.date",
        "completion_date": "protocolSection.statusModule.completionDateStruct.date",
        "brief_summary": "protocolSection.descriptionModule.briefSummary"
    },

    # 通用配置
    "request_delay_seconds": 0.5,
    "max_pages": 10,
    "timeout": 30
}
