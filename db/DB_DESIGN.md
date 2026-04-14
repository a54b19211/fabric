# ZK_Data Fabric — 数据库表设计文档

## 一、设计原则

**单一主表 + 附件子表**，不按来源拆表。

所有来源（NMPA、CDE、FDA、ChiCTR、PubMed、DrugBank、港交所、Fierce Pharma……）的数据统一落入 `documents` 表，通过 `source_site` 字段区分来源。附件（PDF 等）因为是一对多关系，单独放 `attachments` 表。

不拆表的原因：
- 各来源的核心字段高度重叠（标题、日期、摘要、药品名、来源URL）
- 拆表会导致跨来源查询必须 UNION，维护成本高
- 来源特有字段（如 ChiCTR 的注册号、港交所的股票代码）用专用列承载，非该来源的行留空即可
- 新增来源不需要建新表，只需加爬虫配置

---

## 二、表结构

### 2.1 `documents`（主文档表）

所有字段对所有来源通用，无来源特有字段。

| 列名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `uuid` | TEXT | — | 主键，UUID5 基于来源 URL 幂等生成 |
| `title` | TEXT | '' | 文档标题 |
| `publish_date` | TEXT | '' | 发布日期，TEXT 而非 DATE（各来源格式不统一） |
| `category` | TEXT | '' | 分类标签 |
| `summary` | TEXT | '' | 摘要 |
| `drug_name` | TEXT | '' | 涉及药品名称 |
| `approval_number` | TEXT | '' | 批准文号 |
| `manufacturer` | TEXT | '' | 生产企业 |
| `keywords` | TEXT[] | '{}' | 关键词，支持 `WHERE 'GLP-1' = ANY(keywords)` |
| `source_url` | TEXT | '' | 原始网页 URL |
| `source_site` | TEXT | '' | 来源站点标识：nmpa / cde / chictr / pubmed / hkex … |
| `local_file_path` | TEXT | '' | 本地 PDF 路径（本地导入时填） |
| `parent_uuid` | TEXT | '' | 父文档 UUID，附件 PDF 用，业务层保证关联 |
| `crawl_timestamp` | TIMESTAMPTZ | NULL | 采集时间 |
| `confidence_level` | CHAR(1) | 'C' | A / B / C |
| `confidence_score` | REAL | 0.3 | 0~1 |
| `confidence_reason` | TEXT | '' | 打分依据 |
| `parse_method` | TEXT | '' | css_extract / trafilatura+qwen / mineru+qwen |
| `markdown_path` | TEXT | '' | 本地 Markdown 相对路径 |
| `raw_file_path` | TEXT | '' | 本地原始文件相对路径 |
| `status` | TEXT | 'AUTO_PROCESSED' | AUTO_PROCESSED / PENDING_MANUAL_REVIEW / MANUAL_REVIEWED |
| `manual_score` | REAL | NULL | 人工审核打分 |
| `reviewer_note` | TEXT | '' | 审核备注 |
| `minio_bucket` | TEXT | 'zk-data-fabric' | MinIO 预留 |
| `minio_object_key` | TEXT | '' | MinIO 预留 |
| `created_at` | TIMESTAMPTZ | NOW() | 首次入库，upsert 不更新 |
| `updated_at` | TIMESTAMPTZ | NOW() | 触发器自动维护 |

### 2.2 `attachments`（附件表）

无外键约束，通过 `parent_uuid` 与 `documents` 关联，业务层自行保证一致性。

| 列名 | 类型 | 说明 |
|---|---|---|
| `id` | SERIAL | 自增主键 |
| `parent_uuid` | TEXT | 对应 documents.uuid，无外键 |
| `file_name` | TEXT | 附件文件名 |
| `file_url` | TEXT | 附件下载链接 |
| `file_uuid` | TEXT | 附件自身 UUID5 |
| `created_at` | TIMESTAMPTZ | 入库时间 |

### 2.3 `hkex_meta`（港交所扩展表）

仅 `source_site = 'hkex'` 的记录写入此表。

| 列名 | 类型 | 说明 |
|---|---|---|
| `uuid` | TEXT | 主键，对应 documents.uuid |
| `stock_code` | TEXT | 港股代码，如 `00241` |
| `stock_name` | TEXT | 股票名称 |
| `stock_id` | INTEGER | 港交所内部 stockId |
| `created_at` | TIMESTAMPTZ | — |
| `updated_at` | TIMESTAMPTZ | — |

### 2.4 `pubmed_meta`（PubMed 扩展表）

仅 `source_site = 'pubmed'` 的记录写入此表。

| 列名 | 类型 | 说明 |
|---|---|---|
| `uuid` | TEXT | 主键，对应 documents.uuid |
| `doi` | TEXT | 文献 DOI |
| `journal_name` | TEXT | 期刊名 |
| `pmid` | TEXT | PubMed ID |
| `created_at` | TIMESTAMPTZ | — |
| `updated_at` | TIMESTAMPTZ | — |

### 2.5 `chictr_meta`（ChiCTR 扩展表）

仅 `source_site = 'chictr'` 的记录写入此表。

| 列名 | 类型 | 说明 |
|---|---|---|
| `uuid` | TEXT | 主键，对应 documents.uuid |
| `regno` | TEXT | ChiCTR 注册号，如 ChiCTR2600122286 |
| `reg_status` | TEXT | 预注册 / 补注册 |
| `study_type` | TEXT | 研究类型 |
| `study_stage` | TEXT | 研究阶段 |
| `intervention` | TEXT | 干预措施 |
| `condition` | TEXT | 目标疾病 |
| `sample_size` | TEXT | 样本量 |
| `recruitment_status` | TEXT | 招募状态 |
| `primary_sponsor` | TEXT | 主办单位 |
| `ethics_approved` | TEXT | 是否获伦理批准 |
| `ethics_committee` | TEXT | 伦理委员会名称 |
| `ethics_date` | TEXT | 伦理批准日期 |
| `start_date` | TEXT | 预计开始日期 |
| `end_date` | TEXT | 预计完成日期 |
| `created_at` | TIMESTAMPTZ | — |
| `updated_at` | TIMESTAMPTZ | — |

---

## 三、索引设计

| 索引名 | 列 | 原因 |
|---|---|---|
| `idx_documents_source_site` | `source_site` | 按来源过滤是最高频查询 |
| `idx_documents_publish_date` | `publish_date` | 时间范围查询 |
| `idx_documents_status` | `status` | 人工审核队列过滤 |
| `idx_documents_drug_name` | `drug_name` | 按药品名检索 |
| `idx_documents_parent_uuid` | `parent_uuid` | 查附件 PDF 所属父文档 |
| `idx_attachments_parent_uuid` | `parent_uuid` | 查某文档的所有附件 |

---

## 四、来源与字段覆盖关系

| 来源 | source_site | 核心填充字段 | 特有字段 |
|---|---|---|---|
| 国家药监局 | nmpa | title, publish_date, drug_name, approval_number | — |
| 药审中心 | cde | title, publish_date | — |
| FDA 新闻 | fda_press | title, publish_date | — |
| FDA 药品安全 | fda_drug_safety | title, publish_date, drug_name | — |
| FDA 召回 | fda_drug_recalls | title, publish_date, drug_name, manufacturer | — |
| ChiCTR | chictr | title, publish_date, category | — |
| ClinicalTrials.gov | clinicaltrials | title, publish_date, summary, drug_name | — |
| PubMed | pubmed | title, publish_date, summary, doi, journal_name | doi, journal_name |
| DrugBank | drugbank | title, summary, drug_name | — |
| 港交所披露易 | hkex | title, publish_date | stock_code, stock_name, stock_id |
| Fierce Pharma | fiercepharma | title, publish_date, summary | — |
| PHIRDA | phirda | title, publish_date | — |

---

## 五、幂等与 Upsert 策略

`uuid` 由 `uuid.uuid5(NAMESPACE_URL, source_url)` 生成，同一 URL 永远产生同一 UUID。

写入逻辑（`db/repository.py`）：
- 若 `uuid` 不存在 → INSERT
- 若 `uuid` 已存在 → UPDATE 所有字段（`created_at` 除外），同时删除旧附件行、重建新附件行

这保证了重复爬取同一页面不会产生重复数据，只会更新内容。

---

## 六、审核状态流转

```
采集完成
    ↓
confidence_level == 'C'  →  PENDING_MANUAL_REVIEW  →  人工审核  →  MANUAL_REVIEWED
confidence_level == 'A/B' →  AUTO_PROCESSED
```

`PENDING_MANUAL_REVIEW` 的记录会被 `export_review_csv` 导出为 CSV，人工填写 `manual_score` 和 `reviewer_note` 后通过 `feedback_ingest.py` 回灌，状态更新为 `MANUAL_REVIEWED`。
