"""
==========================================================
  数据结构定义（Pydantic 模型）
  ★ 药学研究生专属文件 ★ —— 只需要关注"字段叫什么、填什么"
==========================================================

【使用说明】
- 每个字段旁边都有中文注释，告诉你这个字段是干什么的。
- 需要新增字段？直接模仿已有的写法加一行就行。
- 不需要懂 Python 类继承，BaseModel 就当成"表格模板"理解。
"""

from typing import Optional, List
from pydantic import BaseModel, Field


# ======================== 附件信息 ========================
class AttachmentInfo(BaseModel):
    """当一条公告里有可下载的 PDF 附件时，用这个结构记录"""

    file_name: str = Field(default="", description="附件文件名，例如：说明书.pdf")
    file_url: str = Field(default="", description="附件的下载链接")
    file_uuid: str = Field(default="", description="附件自身的 UUID（自动生成）")


# ======================== 来源血缘追踪 ========================
class ProvenanceInfo(BaseModel):
    """记录这条数据"从哪来的"，方便未来溯源"""

    source_url: str = Field(default="", description="原始网页 URL（爬取时自动填）")
    source_site: str = Field(default="", description="来源站点标识，如 nmpa / cde")
    local_file_path: str = Field(default="", description="本地 PDF 原始路径（本地导入时填）")
    parent_uuid: str = Field(default="", description="父公告 UUID（附件 PDF 用，建立母子血缘）")
    crawl_timestamp: str = Field(default="", description="采集时间 ISO 格式")


# ======================== 置信度评估 ========================
class ConfidenceInfo(BaseModel):
    """三级置信度评估结果"""

    level: str = Field(default="C", description="置信度等级：A=高(0.8~1.0) / B=中(0.5~0.7) / C=低(0.3)")
    score: float = Field(default=0.3, description="置信度分数 0~1")
    reason: str = Field(default="", description="打分依据说明")


# ======================== JSON 元数据信封（核心！）========================
class MetadataEnvelope(BaseModel):
    """
    ★ 最终落盘的 JSON 信封 ★
    三位一体中的"结构化 JSON"就是这个模型序列化后的结果。
    """

    # --- 唯一标识 ---
    uuid: str = Field(description="基于 URL/文件路径生成的 UUID5，保证幂等")

    # --- 基本信息 ---
    title: str = Field(default="", description="文档标题")
    publish_date: str = Field(default="", description="发布日期")
    category: str = Field(default="", description="分类标签，如：通告 / 公告 / 文献")
    summary: str = Field(default="", description="摘要（可由 Qwen 生成）")

    # --- 结构化提取字段（药学业务按需扩展）---
    drug_name: str = Field(default="", description="涉及药品名称")
    approval_number: str = Field(default="", description="批准文号")
    manufacturer: str = Field(default="", description="生产企业")
    keywords: List[str] = Field(default_factory=list, description="关键词列表")

    # --- 血缘信息 ---
    provenance: ProvenanceInfo = Field(default_factory=ProvenanceInfo)

    # --- 置信度 ---
    confidence: ConfidenceInfo = Field(default_factory=ConfidenceInfo)

    # --- 附件列表 ---
    attachments: List[AttachmentInfo] = Field(default_factory=list)

    # --- 解析元信息 ---
    parse_method: str = Field(
        default="",
        description="解析方式：css_extract / trafilatura / mineru+qwen",
    )
    markdown_path: str = Field(default="", description="对应 Markdown 文件相对路径")
    raw_file_path: str = Field(default="", description="对应原始文件相对路径")

    # --- 审核状态 ---
    status: str = Field(
        default="AUTO_PROCESSED",
        description="状态：AUTO_PROCESSED / PENDING_MANUAL_REVIEW / MANUAL_REVIEWED",
    )
    manual_score: Optional[float] = Field(
        default=None, description="人工审核打分（回灌时填入）"
    )
    reviewer_note: str = Field(default="", description="审核人员备注")

    # --- MinIO 船票（二期预留）---
    future_minio_bucket: str = Field(default="zk-data-fabric", description="预留 MinIO 桶名")
    future_minio_object_key: str = Field(default="", description="预留 MinIO 对象键")

    # --- RAG 字段 ---
    doc_type: str = Field(default="", description="文档类型：regulatory_notice / drug_recall / drug_label / clinical_trial / literature / industry_news / company_announcement")
    language: str = Field(default="", description="语言：zh / en")
