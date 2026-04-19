"""
SQLAlchemy ORM 模型 —— 与 db/schema.sql 保持一致。
无外键约束，关联由业务层通过 uuid 自行保证。
"""
from datetime import datetime
from typing import Optional, List

from sqlalchemy import String, Text, Float, Integer, DateTime, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "documents"

    uuid: Mapped[str] = mapped_column(String, primary_key=True)

    # 基本信息
    title: Mapped[str] = mapped_column(Text, default="")
    publish_date: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")

    # 药学通用业务字段
    drug_name: Mapped[str] = mapped_column(Text, default="")
    approval_number: Mapped[str] = mapped_column(Text, default="")
    manufacturer: Mapped[str] = mapped_column(Text, default="")
    keywords: Mapped[List[str]] = mapped_column(ARRAY(Text), default=list)

    # 来源血缘
    source_url: Mapped[str] = mapped_column(Text, default="")
    source_site: Mapped[str] = mapped_column(Text, default="")
    local_file_path: Mapped[str] = mapped_column(Text, default="")
    parent_uuid: Mapped[str] = mapped_column(Text, default="")
    crawl_timestamp: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # 置信度
    confidence_level: Mapped[str] = mapped_column(String(1), default="C")
    confidence_score: Mapped[float] = mapped_column(Float, default=0.3)
    confidence_reason: Mapped[str] = mapped_column(Text, default="")

    # 解析元信息
    parse_method: Mapped[str] = mapped_column(Text, default="")
    markdown_path: Mapped[str] = mapped_column(Text, default="")
    raw_file_path: Mapped[str] = mapped_column(Text, default="")

    # 审核状态
    status: Mapped[str] = mapped_column(Text, default="AUTO_PROCESSED")
    manual_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    reviewer_note: Mapped[str] = mapped_column(Text, default="")

    # MinIO 预留
    minio_bucket: Mapped[str] = mapped_column(Text, default="zk-data-fabric")
    minio_object_key: Mapped[str] = mapped_column(Text, default="")

    # RAG 字段
    doc_type: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str] = mapped_column(Text, default="")
    weknora_sync_status: Mapped[str] = mapped_column(Text, default="pending")

    # 系统字段
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent_uuid: Mapped[str] = mapped_column(Text, nullable=False)  # 无外键，业务层保证
    file_name: Mapped[str] = mapped_column(Text, default="")
    file_url: Mapped[str] = mapped_column(Text, default="")
    file_uuid: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HkexMeta(Base):
    __tablename__ = "hkex_meta"

    uuid: Mapped[str] = mapped_column(String, primary_key=True)
    stock_code: Mapped[str] = mapped_column(Text, default="")
    stock_name: Mapped[str] = mapped_column(Text, default="")
    stock_id: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class PubmedMeta(Base):
    __tablename__ = "pubmed_meta"

    uuid: Mapped[str] = mapped_column(String, primary_key=True)
    doi: Mapped[str] = mapped_column(Text, default="")
    journal_name: Mapped[str] = mapped_column(Text, default="")
    pmid: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ChictrMeta(Base):
    __tablename__ = "chictr_meta"

    uuid: Mapped[str] = mapped_column(String, primary_key=True)
    regno: Mapped[str] = mapped_column(Text, default="")
    reg_status: Mapped[str] = mapped_column(Text, default="")
    study_type: Mapped[str] = mapped_column(Text, default="")
    study_stage: Mapped[str] = mapped_column(Text, default="")
    intervention: Mapped[str] = mapped_column(Text, default="")
    condition: Mapped[str] = mapped_column(Text, default="")
    sample_size: Mapped[str] = mapped_column(Text, default="")
    recruitment_status: Mapped[str] = mapped_column(Text, default="")
    primary_sponsor: Mapped[str] = mapped_column(Text, default="")
    ethics_approved: Mapped[str] = mapped_column(Text, default="")
    ethics_committee: Mapped[str] = mapped_column(Text, default="")
    ethics_date: Mapped[str] = mapped_column(Text, default="")
    start_date: Mapped[str] = mapped_column(Text, default="")
    end_date: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
