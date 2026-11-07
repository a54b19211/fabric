"""
图谱实体字段 Schema 定义（Pydantic 模型）

每种实体类型对应一个模型，字段与 Neo4j 节点定义严格对齐。
用途：
  1. 作为 LLM 抽取 prompt 中的字段约束说明
  2. 对 LLM 输出的 entity_data 做结构校验
  3. 推送 Neo4j 时的数据规范
"""
from typing import Optional

from loguru import logger
from pydantic import BaseModel, Field


class CompanyEntity(BaseModel):
    id: str = Field(default="", description="统一社会信用代码（国内）/ LEI码（国际）")
    name: str = Field(default="", description="标准名称（中文）")
    name_en: str = Field(default="", description="英文名")
    aliases: list[str] = Field(default_factory=list, description="别名列表（简称/曾用名/股票代码）")
    types: list[str] = Field(default_factory=list, description="公司角色：biotech/pharma/cro/cmo/investment/regulator/academic")
    country: str = Field(default="", description="国家")
    founded_year: Optional[int] = Field(default=None, description="成立年份")
    stage: str = Field(default="", description="早期研发/临床阶段/商业化阶段")
    primary_focus: str = Field(default="", description="主要治疗领域")
    stock_code: str = Field(default="", description="股票代码")
    exchange: str = Field(default="", description="A股/港股/NASDAQ/NYSE")
    market_cap: Optional[float] = Field(default=None, description="当前市值快照（USD）")
    market_cap_date: str = Field(default="", description="市值更新日期")


class DrugEntity(BaseModel):
    id: str = Field(default="", description="DrugBank ID / NMPA批准文号 / 内部ID")
    name: str = Field(default="", description="通用名（INN名优先）")
    name_en: str = Field(default="", description="英文通用名；若 entity_name 为英文则直接填入；若中文则翻译为英文")
    aliases: list[str] = Field(default_factory=list, description="商品名/研发代号/曾用名")
    drug_class: str = Field(default="", description="化学药品/生物制品/中药")
    modality: str = Field(default="", description="小分子/单抗/双抗/ADC/多肽/融合蛋白/纳米抗体/细胞治疗/基因治疗/核酸药物/反义寡核苷酸/小干扰RNA/放射性药物/疫苗/溶瘤病毒")
    innovation_type: str = Field(default="", description="创新药/仿制药/改良型新药/生物类似药")
    innovation_labels: list[str] = Field(default_factory=list, description="first-in-class/best-in-class/me-too/me-better")
    global_status: str = Field(default="", description="Active/Discontinued/Unknown")
    phase_cn: str = Field(default="", description="临床前/IND申报/I期/II期/III期/NDA申报/已上市/终止/暂停")
    phase_us: str = Field(default="", description="同上，美国")
    phase_eu: str = Field(default="", description="同上，欧洲")
    phase_jp: str = Field(default="", description="同上，日本")
    cas_number: str = Field(default="", description="CAS号（小分子用）")
    registration_number: str = Field(default="", description="登记号（如Y20190009999）/ 备案号")
    patent_expiry_cn: str = Field(default="", description="中国化合物专利到期日")
    patent_expiry_us: str = Field(default="", description="美国化合物专利到期日")


class TargetEntity(BaseModel):
    id: str = Field(default="", description="UniProt ID / HGNC ID")
    name: str = Field(default="", description="官方基因名/蛋白名（如 CLDN18.2）")
    name_en: str = Field(default="", description="英文名/基因符号；若 entity_name 为英文则直接填入；若中文则翻译为英文")
    gene_symbol: str = Field(default="", description="基因符号（如 CLDN18）")
    aliases: list[str] = Field(default_factory=list, description="别名/曾用名")
    target_type: str = Field(default="", description="膜蛋白/GPCR/酶/转录因子/离子通道/免疫检查点/表面抗原/RNA")
    target_class: str = Field(default="", description="成熟靶点/新兴靶点/前沿靶点")
    druggability_score: Optional[float] = Field(default=None, description="成药性评分 0-1")
    validation_level: str = Field(default="", description="临床验证/临床前验证/预测")
    species: str = Field(default="human", description="human/mouse/rat")


class IndicationEntity(BaseModel):
    id: str = Field(default="", description="MeSH ID / ICD-11编码")
    name: str = Field(default="", description="适应症名称")
    name_en: str = Field(default="", description="英文名；若 entity_name 为英文则直接填入；若中文则翻译为英文")
    aliases: list[str] = Field(default_factory=list, description="别名")
    icd_code: str = Field(default="", description="ICD编码")
    category: str = Field(default="", description="治疗领域：肿瘤/心血管/神经/内分泌/感染/免疫/罕见病/消化")
    entity_category: str = Field(default="indication", description="实体语义类型：disease（独立疾病名称，如流行病学报道中的病名）或 indication（药物批准/声称治疗的适应症）")
    is_rare: bool = Field(default=False, description="是否罕见病")


class ClinicalTrialEntity(BaseModel):
    id: str = Field(default="", description="内部ID")
    trial_id: str = Field(default="", description="NCT / CTR编号")
    phase: str = Field(default="", description="I/II/III/IV/I-II/II-III")
    status: str = Field(default="", description="招募中/进行中/完成/终止/暂停")
    start_date: str = Field(default="", description="开始日期")
    end_date: str = Field(default="", description="结束日期")
    region: list[str] = Field(default_factory=list, description="试验地区")
    primary_endpoint: str = Field(default="", description="主要终点")
    sample_size: Optional[int] = Field(default=None, description="样本量")
    blinding: str = Field(default="", description="开放/单盲/双盲")
    has_result: bool = Field(default=False, description="是否有结果数据")


class DealEventEntity(BaseModel):
    id: str = Field(default="", description="内部ID")
    deal_category: str = Field(default="", description="融资/许可/并购/战略合作")
    deal_sub_types: list[str] = Field(default_factory=list, description="子类型")
    upfront_usd: Optional[float] = Field(default=None, description="首付款（美元）")
    milestone_usd: Optional[float] = Field(default=None, description="里程碑总额（美元）")
    total_amount_usd: Optional[float] = Field(default=None, description="总对价（美元）")
    currency_orig: str = Field(default="", description="原始货币")
    amount_orig: Optional[float] = Field(default=None, description="原始金额")
    exchange_rate_date: str = Field(default="", description="汇率取值日期")
    rights_region: list[str] = Field(default_factory=list, description="权益地区")
    exclusivity_type: str = Field(default="", description="独家/非独家")
    date: str = Field(default="", description="交易日期")
    status: str = Field(default="", description="达成/终止/传闻/修订")
    termination_reason: str = Field(default="", description="终止原因")


class ApprovalEventEntity(BaseModel):
    id: str = Field(default="", description="内部ID")
    application_type: str = Field(default="", description="NDA/BLA/IND/sNDA/sBLA/MAA")
    approval_type: str = Field(default="", description="常规批准/附条件批准/紧急使用授权")
    designations: list[str] = Field(default_factory=list, description="突破性疗法/优先审评/孤儿药/快速通道")
    approval_number: str = Field(default="", description="批准文号（如国药准字）")
    status: str = Field(default="", description="申报中/审评中/获批/拒绝/撤回")
    submit_date: str = Field(default="", description="申报日期")
    approval_date: str = Field(default="", description="批准日期")
    indication_text: str = Field(default="", description="申报适应症文本")
    conditions: list[str] = Field(default_factory=list, description="附加条件")


class PolicyEntity(BaseModel):
    id: str = Field(default="", description="内部ID")
    title: str = Field(default="", description="政策标题")
    policy_type: str = Field(default="", description="集采/医保目录/审批改革/临床指南/价格管控/鼓励政策/监管规范")
    issue_date: str = Field(default="", description="发布日期")
    effective_date: str = Field(default="", description="生效日期")
    doc_number: str = Field(default="", description="文号")
    affected_regions: list[str] = Field(default_factory=list, description="影响地区")


class SafetyEventEntity(BaseModel):
    id: str = Field(default="", description="内部ID")
    event_type: str = Field(default="", description="不良事件/安全警示/黑框警告/上市后监测")
    safety_signal: str = Field(default="", description="安全信号描述")
    severity: str = Field(default="", description="严重/中等/轻微")
    affected_population: str = Field(default="", description="受影响人群")
    report_date: str = Field(default="", description="通报日期")
    source_agency: str = Field(default="", description="来源机构（FDA/EMA/NMPA等）")
    recommendation: str = Field(default="", description="机构建议措施")


class RecallEventEntity(BaseModel):
    id: str = Field(default="", description="内部ID")
    recall_class: str = Field(default="", description="召回级别（I/II/III）")
    recall_reason: str = Field(default="", description="召回原因")
    recall_date: str = Field(default="", description="召回日期")
    affected_batches: list[str] = Field(default_factory=list, description="受影响批次")
    affected_regions: list[str] = Field(default_factory=list, description="召回地区")
    recall_scope: str = Field(default="", description="零售/批发/用户级")
    recall_status: str = Field(default="", description="进行中/已完成")


class TCMFormulaEntity(BaseModel):
    id: str = Field(default="", description="内部ID")
    name: str = Field(default="", description="方剂名称")
    aliases: list[str] = Field(default_factory=list, description="别名")
    formula_type: str = Field(default="", description="中成药/经典方/提取物/单味药")
    standard: str = Field(default="", description="药典标准")
    functions: str = Field(default="", description="功效描述")


class CompoundEntity(BaseModel):
    id: str = Field(default="", description="内部ID")
    name: str = Field(default="", description="化合物名称")
    cas_number: str = Field(default="", description="CAS号")
    molecular_formula: str = Field(default="", description="分子式")
    compound_type: str = Field(default="", description="生物碱/黄酮/皂苷/多糖/萜类")


class PathwayEntity(BaseModel):
    id: str = Field(default="", description="KEGG pathway ID（如 hsa04151）")
    name: str = Field(default="", description="通路名称")
    name_en: str = Field(default="", description="英文名；若 entity_name 为英文则直接填入；若中文则翻译为英文")
    aliases: list[str] = Field(default_factory=list, description="别名")
    pathway_type: str = Field(default="", description="信号通路/代谢通路/免疫通路")


class DealItemEntity(BaseModel):
    """交易条款明细，挂在 DealEvent 下，通过 HAS_ITEM 关联"""
    id: str = Field(default="", description="内部ID")
    item_type: str = Field(default="", description="条款类型：药物权益 / 技术平台权益")
    rights_regions: list[str] = Field(default_factory=list, description="该条款对应的权益地区（如[\"美国\",\"欧洲\",\"日本\"]）")


class DevelopmentProjectEntity(BaseModel):
    """合作开发项目，多家公司共同研发某药物的项目实体"""
    id: str = Field(default="", description="内部ID")
    name: str = Field(default="", description='项目名称（如"A公司与B公司SYSA1801联合开发项目"）')
    start_date: str = Field(default="", description="合作开始日期")
    end_date: str = Field(default="", description="合作终止日期（可选）")
    status: str = Field(default="", description="进行中 / 完成 / 终止")


class TrialIndicationEntity(BaseModel):
    """临床试验中的具体适应症条目，挂在 ClinicalTrial 下"""
    id: str = Field(default="", description="内部ID")
    phase: str = Field(default="", description="该适应症对应的试验阶段：I / II / III")
    status: str = Field(default="", description="该适应症的试验状态：进行中 / 完成 / 终止")
    orr: Optional[float] = Field(default=None, description="客观缓解率（可选，有结果时填写）")
    pfs_months: Optional[float] = Field(default=None, description="无进展生存期（月，可选）")
    os_months: Optional[float] = Field(default=None, description="总生存期（月，可选）")
    result_summary: str = Field(default="", description="结果摘要文本（可选）")


class OrganizationEntity(BaseModel):
    """A named source organization; registration does not prove it is a company."""
    name: str = ""
    source_fields: dict = Field(default_factory=dict)


class InterventionEntity(BaseModel):
    """Registered interventions can be drugs, procedures, devices or other types."""
    name: str = ""
    source_fields: dict = Field(default_factory=dict)


class RegulatoryApplicationEntity(BaseModel):
    application_number: str = ""
    application_number_raw: str = ""
    authority: str = ""
    source_fields: dict = Field(default_factory=dict)


class RecallProductEntity(BaseModel):
    """FDA recall_number identifies one recalled product, not the whole event."""
    recall_number: str = ""
    event_id: Optional[str] = None
    classification: Optional[str] = None
    status: Optional[str] = None
    recall_initiation_date: Optional[str] = None
    termination_date: Optional[str] = None


# ── 实体类型 → Pydantic 模型映射 ──────────────────────────────────────────
ENTITY_SCHEMA_MAP: dict[str, type[BaseModel]] = {
    "Company":            CompanyEntity,
    "Drug":               DrugEntity,
    "Target":             TargetEntity,
    "Indication":         IndicationEntity,
    "ClinicalTrial":      ClinicalTrialEntity,
    "DealEvent":          DealEventEntity,
    "DealItem":           DealItemEntity,
    "ApprovalEvent":      ApprovalEventEntity,
    "Policy":             PolicyEntity,
    "SafetyEvent":        SafetyEventEntity,
    "RecallEvent":        RecallEventEntity,
    "TCMFormula":         TCMFormulaEntity,
    "Compound":           CompoundEntity,
    "Pathway":            PathwayEntity,
    "DevelopmentProject": DevelopmentProjectEntity,
    "TrialIndication":    TrialIndicationEntity,
    "Organization":       OrganizationEntity,
    "Intervention":       InterventionEntity,
    "RegulatoryApplication": RegulatoryApplicationEntity,
    "RecallProduct":      RecallProductEntity,
}

VALID_ENTITY_TYPES = set(ENTITY_SCHEMA_MAP.keys())


def validate_entity_data(entity_type: str, raw: dict) -> dict:
    """
    用对应 Pydantic 模型校验并规范化 entity_data。
    未知类型原样返回，不抛异常。
    """
    model_cls = ENTITY_SCHEMA_MAP.get(entity_type)
    if model_cls is None:
        return _sanitize_entity_data(raw)
    try:
        return model_cls(**raw).model_dump()
    except Exception as e:
        logger.warning(f"[Schema] entity_type={entity_type} 校验失败，保留原始数据: {e}")
        return _sanitize_entity_data(raw)


def _sanitize_entity_data(data: dict) -> dict:
    """防止 LLM 返回 null 值导致下游 for 循环迭代 None。"""
    for key in ("aliases",):
        if key in data and data[key] is None:
            data[key] = []
    return data


def get_entity_schema_description(entity_type: str) -> str:
    """
    生成供 prompt 使用的字段说明文本。
    """
    model_cls = ENTITY_SCHEMA_MAP.get(entity_type)
    if model_cls is None:
        return ""
    lines = []
    for field_name, field_info in model_cls.model_fields.items():
        desc = field_info.description or ""
        annotation = field_info.annotation
        type_str = getattr(annotation, "__name__", str(annotation))
        lines.append(f"  {field_name} ({type_str}): {desc}")
    return "\n".join(lines)
