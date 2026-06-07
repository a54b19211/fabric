"""
数据结构定义（Pydantic 模型）

【使用说明】
- 每个字段旁边都有中文注释，告诉你这个字段是干什么的。
- 需要新增字段？直接模仿已有的写法加一行就行。
- BaseModel 就当成"表格模板"理解。

【extra 字段设计原则】
- documents 主表只保留所有来源通用的字段
- 来源特有字段统一放入 extra（写入 doc_extra 表的 JSONB）
- 每个来源定义自己的 Extra Pydantic 模型，在爬虫写入时做结构验证
- 数据库层不做结构约束，约束在代码层
"""

from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


# ======================== 附件信息 ========================
class AttachmentInfo(BaseModel):
    file_name: str = Field(default="", description="附件文件名，例如：说明书.pdf")
    file_url: str = Field(default="", description="附件的下载链接")
    file_uuid: str = Field(default="", description="附件自身的 UUID（自动生成）")
    local_path: str = Field(default="", description="附件本地存储路径（下载后填入）")


# ======================== 来源血缘追踪 ========================
class ProvenanceInfo(BaseModel):
    source_url: str = Field(default="", description="原始网页 URL")
    source_site: str = Field(default="", description="来源站点标识，如 nmpa / cde")
    local_file_path: str = Field(default="", description="本地 PDF 原始路径（本地导入时填）")
    parent_uuid: str = Field(default="", description="父公告 UUID（附件 PDF 用）")
    crawl_timestamp: str = Field(default="", description="采集时间 ISO 格式")


# ======================== 置信度评估 ========================
class ConfidenceInfo(BaseModel):
    score: float = Field(default=0.3, description="置信度分数 0~1")
    reason: str = Field(default="", description="打分依据说明")


# ======================== 各来源 Extra 模型 ========================
# 每个来源定义自己的 extra 结构，爬虫写入时用对应模型验证后序列化为 dict

class NmpaExtra(BaseModel):
    """国家药品监督管理局（NMPA）特有字段"""
    drug_name: str = Field(default="", description="涉及药品名称")
    approval_number: str = Field(default="", description="批准文号")
    manufacturer: str = Field(default="", description="生产企业")


class CdeExtra(BaseModel):
    """国家药品审评中心（CDE）特有字段"""
    drug_name: str = Field(default="", description="涉及药品名称")
    approval_number: str = Field(default="", description="批准文号")
    manufacturer: str = Field(default="", description="生产企业")


class ChictrExtra(BaseModel):
    """中国临床试验注册中心（ChiCTR）特有字段"""
    regno: str = Field(default="", description="注册号，如 ChiCTR2600122286")
    reg_status: str = Field(default="", description="注册号状态")
    study_type: str = Field(default="", description="研究类型")
    study_stage: str = Field(default="", description="研究阶段")
    study_design: str = Field(default="", description="研究设计")
    objective: str = Field(default="", description="研究目的")
    intervention: str = Field(default="", description="干预措施")
    condition: str = Field(default="", description="目标疾病")
    inclusion_criteria: str = Field(default="", description="纳入标准")
    exclusion_criteria: str = Field(default="", description="排除标准")
    sample_size: str = Field(default="", description="样本量")
    recruitment_status: str = Field(default="", description="招募状态")
    primary_sponsor: str = Field(default="", description="研究实施负责单位")
    start_date: str = Field(default="", description="预计研究开始日期")
    end_date: str = Field(default="", description="预计研究完成日期")
    funding_source: str = Field(default="", description="经费或物资来源")
    ethics_approved: str = Field(default="", description="是否获伦理委员会批准")


class PubmedExtra(BaseModel):
    """PubMed 特有字段"""
    pmid: str = Field(default="", description="PubMed ID")
    pmcid: str = Field(default="", description="PubMed Central ID")
    doi: str = Field(default="", description="DOI")
    journal_name: str = Field(default="", description="期刊名称")
    authors: List[str] = Field(default_factory=list, description="作者列表")
    fulltext_status: str = Field(default="abstract_only", description="全文获取状态：pmc_fulltext / unpaywall_pdf / abstract_only")
    oa_pdf_url: str = Field(default="", description="Open Access PDF 链接")


class HkexExtra(BaseModel):
    """香港交易所（HKEX）特有字段"""
    stock_code: str = Field(default="", description="股票代码，如 01093")
    stock_name: str = Field(default="", description="股票名称，如 石药集团")
    stock_id: Optional[int] = Field(default=None, description="HKEX 内部股票 ID")


class DrugbankExtra(BaseModel):
    """DrugBank 特有字段"""
    drugbank_id: str = Field(default="", description="DrugBank ID，如 DB12345")
    drug_type: str = Field(default="", description="药物类型：small molecule / biologic")
    state: str = Field(default="", description="物理状态：solid / liquid / gas")
    groups: List[str] = Field(default_factory=list, description="审批状态：approved / experimental / withdrawn 等")
    categories: List[str] = Field(default_factory=list, description="药物分类")
    atc_codes: List[str] = Field(default_factory=list, description="ATC 编码列表")
    cas_number: str = Field(default="", description="CAS 号")
    unii: str = Field(default="", description="UNII 编码")
    synonyms: List[str] = Field(default_factory=list, description="同义词列表（最多10个）")


class ClinicalTrialsExtra(BaseModel):
    """ClinicalTrials.gov 特有字段"""
    nct_id: str = Field(default="", description="NCT编号，如 NCT00001372")
    study_type: str = Field(default="", description="研究类型：Interventional / Observational")
    phase: List[str] = Field(default_factory=list, description="研究阶段：Phase 1 / Phase 2 / Phase 3 / Phase 4")
    study_status: str = Field(default="", description="研究状态：Recruiting / Completed / Terminated")
    enrollment: int = Field(default=0, description="计划入组人数")
    study_completion_date: str = Field(default="", description="研究完成日期")
    primary_completion_date: str = Field(default="", description="主要终点完成日期")
    sponsor: str = Field(default="", description="申办方")
    collaborators: List[str] = Field(default_factory=list, description="合作方列表")
    conditions: List[str] = Field(default_factory=list, description="研究疾病/条件列表")
    interventions: List[str] = Field(default_factory=list, description="干预措施列表")
    arm_groups: List[str] = Field(default_factory=list, description="研究组别列表")


class FdaEnforcementExtra(BaseModel):
    """FDA 药品召回（openFDA enforcement）特有字段"""
    recall_number: str = Field(default="", description="召回编号")
    recall_initiation_date: str = Field(default="", description="召回发起日期")
    recall_class: str = Field(default="", description="召回级别：Class I / II / III")
    product_description: str = Field(default="", description="产品描述")
    reason_for_recall: str = Field(default="", description="召回原因")
    status: str = Field(default="", description="召回状态：Ongoing / Terminated / Completed")
    recalling_firm: str = Field(default="", description="召回企业")
    distribution_pattern: str = Field(default="", description="分销范围")


class FdaDrugSafetyExtra(BaseModel):
    """FDA 药品安全通报特有字段"""
    safety_report_id: str = Field(default="", description="安全报告 ID")
    report_date: str = Field(default="", description="报告日期")
    drug_names: List[str] = Field(default_factory=list, description="涉及药品名称列表")


# 来源 → Extra 模型的映射，供 storage_writer 等使用
EXTRA_MODEL_MAP: Dict[str, type] = {
    "nmpa": NmpaExtra,
    "cde": CdeExtra,
    "chictr": ChictrExtra,
    "pubmed": PubmedExtra,
    "hkex": HkexExtra,
    "drugbank": DrugbankExtra,
    "fda_enforcement": FdaEnforcementExtra,
    "fda_drug_safety": FdaDrugSafetyExtra,
    "clinicaltrials": ClinicalTrialsExtra,
}


def validate_extra(source_site: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    按来源验证并规范化 extra 字段。
    找不到对应模型时原样返回（兼容未定义来源）。
    """
    model_cls = EXTRA_MODEL_MAP.get(source_site)
    if model_cls is None:
        return raw
    return model_cls(**raw).model_dump()


# ======================== 原始文档信封（核心）========================
class MetadataEnvelope(BaseModel):
    """爬虫产出的最终数据结构，落盘为 JSON 信封，同时写入 documents 表。

    主表只保留所有来源通用的字段。
    来源特有字段（drug_name / approval_number / pmid / stock_code 等）
    统一放入 extra，写入 doc_extra 表。
    """

    # 唯一标识
    uuid: str = Field(description="基于 URL/文件路径生成的 UUID5，保证幂等")

    # 基本信息
    title: str = Field(default="", description="文档标题")
    publish_date: str = Field(default="", description="发布日期 YYYY-MM-DD")
    category: str = Field(default="", description="分类标签，如：通告 / 公告 / 文献")
    summary: str = Field(default="", description="摘要")
    raw_content: str = Field(default="", description="原文全文（Markdown格式），直接入库")

    # 血缘信息
    provenance: ProvenanceInfo = Field(default_factory=ProvenanceInfo)

    # 置信度
    confidence: ConfidenceInfo = Field(default_factory=ConfidenceInfo)

    # 附件列表
    attachments: List[AttachmentInfo] = Field(default_factory=list)

    # 来源特有字段（写入 doc_extra 表）
    # 写入前请用 validate_extra(source_site, extra) 做结构验证
    extra: Dict[str, Any] = Field(default_factory=dict, description="来源特有字段，写入 doc_extra 表")

    # 解析元信息
    parse_method: str = Field(default="", description="解析方式：css_extract / trafilatura / mineru+qwen")

    # 审核状态
    status: str = Field(
        default="AUTO_PROCESSED",
        description="AUTO_PROCESSED / PENDING_REVIEW / REVIEWED",
    )
    reviewer_note: str = Field(default="", description="审核备注")

    # 文档分类
    doc_type: str = Field(
        default="",
        description="文档类型：regulatory_notice / drug_recall / drug_label / "
                    "clinical_trial / literature / industry_news / company_announcement / policy",
    )
    language: str = Field(default="", description="语言：zh / en")


# ======================== 实体抽取结果 ========================
class ExtractedEntityRecord(BaseModel):
    """LLM 抽取的单个图节点，对应 extracted_entities 表一行。"""

    entity_type: str = Field(description="节点类型：Company/Drug/Target/Indication/ClinicalTrial/DealEvent/ApprovalEvent/Policy/...")
    entity_name: str = Field(default="", description="实体名称（用于消歧展示）")
    entity_data: Dict[str, Any] = Field(default_factory=dict, description="完整字段，对应文档中各节点定义")
    confidence_score: float = Field(default=0.0, description="置信度 0~1")
    confidence_reason: str = Field(default="", description="置信度说明")


class ExtractedRelationRecord(BaseModel):
    """LLM 抽取的单条图边，对应 entity_relations 表一行。"""

    from_entity_idx: int = Field(description="来源实体在本次抽取列表中的下标（0-based）")
    to_entity_idx: int = Field(description="目标实体在本次抽取列表中的下标（0-based）")
    relation_type: str = Field(description="边类型：DEVELOPS/TARGETS/TREATS/PARTICIPATES_IN/...")
    relation_props: Dict[str, Any] = Field(default_factory=dict, description="边属性")
    confidence_score: float = Field(default=0.0, description="置信度 0~1")
    confidence_reason: str = Field(default="", description="置信度说明")


class ExtractionResult(BaseModel):
    """LLM 对单篇文档的完整抽取结果。"""

    source_doc_uuid: str = Field(description="来源文档 uuid")
    source_site: str = Field(default="", description="来源站点")
    model: str = Field(default="", description="使用的模型名称")
    entities: List[ExtractedEntityRecord] = Field(default_factory=list)
    relations: List[ExtractedRelationRecord] = Field(default_factory=list)
    extraction_error: str = Field(default="", description="抽取失败原因（成功时为空）")
