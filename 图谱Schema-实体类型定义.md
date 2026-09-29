# 医药监管知识图谱 Schema —— 实体类型定义与判定标准

> 目的：统一 16 种实体类型的定义、判定边界、命名规范，作为抽取、标注、消歧的共同依据。
> 配套代码：`biz_rules/entity_schemas.py`（字段定义）、`biz_rules/extraction_prompts.py`（抽取规则与 few-shot）
> 最后更新：2026-09-18

---

## 一、类型总览（16 类，三大类）

### A. 实体型（7 类）—— 有稳定身份、可跨文档对齐、消歧主对象

| 类型 | 定义 | 命名锚点 |
|---|---|---|
| Company | 受监管的经营/机构实体 | 完整官方名 |
| Drug | 药品（活性成分/制剂） | INN 通用名 |
| Target | 靶点（基因/蛋白） | HGNC 基因符号 |
| Indication | 适应症/疾病 | ICD/MeSH 标准名 |
| Pathway | 生物通路 | 通路标准名 |
| Compound | 化合物（中药活性成分/化学单体） | 化合物名 |
| TCMFormula | 中药方剂/饮片 | 方剂名 |

### B. 事件/实例型（7 类）—— 每次都是新实例，天然 new_entity，step_none 正常

| 类型 | 定义 | 命名锚点 |
|---|---|---|
| ApprovalEvent | 审批事件（批准/注销/批准文号） | 药品名 + 事件类型 |
| SafetyEvent | 安全事件（不良反应/安全警示） | 药品名 + 事件类型 |
| RecallEvent | 召回事件 | 药品名 + 召回 |
| DealEvent | 交易事件（融资/许可/并购） | 交易名/金额 |
| Policy | 政策 | 政策标题 |
| ClinicalTrial | 临床试验注册 | NCT/CTR 注册号 |
| DevelopmentProject | 研发项目/科技专项 | 项目名 |

### C. 附属型（2 类）—— 挂靠其他实体，不独立存在

| 类型 | 定义 | 挂靠 | 命名锚点 |
|---|---|---|---|
| DealItem | 交易条款明细 | DealEvent（HAS_ITEM） | 药物权益/技术平台权益 |
| TrialIndication | 试验适应症臂 | ClinicalTrial（EVALUATES） | 注册号-适应症-分期 |

---

## 二、各类判定标准（详细）

### 1. Company —— 受监管的经营/机构实体

**判定**：医药监管数据中「参与监管行为或受监管」的单位，含：
- 研发/生产企业（biotech/pharma/cro/cmo）
- 经营/零售企业（药房、药店、连锁公司）
- 监管主体（药监局、检验所、审评中心）
- 医疗机构（医院、制剂室）
- 投资方/学术机构

**命名**：完整官方名 + 股票代码（如 `Roche Holding AG (ROG.SW)`）。

**排除**：非医药公司（律所/会计所，除非是交易中介）；泛称（「这家公司」）。

**易混淆**：药房/医院/监管局属 Company（受监管单位），不是「识别错误」；但其 `types` 枚举缺「零售/监管/检验」档位，是字段粒度问题，可后续补。

### 2. Drug —— 药品

**判定**：有明确药理作用的药物，INN 通用名优先。

**命名**：INN 通用名 + 关键修饰（如 `liposomal doxorubicin`）。

**关键规则（制剂拆解 CONTAINS）**：中文药名带剂型后缀（片/胶囊/注射液…）时，需拆出两个 Drug——制剂 `酚酞片` 与活性成分 `酚酞`，关系 `酚酞片 -[CONTAINS]-> 酚酞`。

> **本体粒度说明**：Drug 同时承载「药品（制剂）」与「药物（活性成分）」两个粒度（`酚酞片` 是制剂、`酚酞` 是活性成分），靠 CONTAINS 关系区分、不拆独立类型。**消歧时两者是不同实体、不得合并**；跨库对齐只发生在「活性成分层」，制剂层（药品）是本图谱的监管特有本体，不与学术知识图谱对齐。

**排除**：复方制剂不拆解（`氨酚伪麻片`）；辅料溶媒不抽（`葡萄糖注射液`/`氯化钠注射液`/`注射用水`）。

**易混淆**：
- vs Compound：Drug 是成药，Compound 是中药活性成分/化学单体
- vs TCMFormula：Drug 是化学药/生物制品，中药归 TCMFormula

### 3. Target —— 靶点

**判定**：药物作用的基因/蛋白靶点。

**命名**：HGNC 基因符号（`EGFR` / `CLDN18.2`），膜蛋白可带别名。

**易混淆**：基因符号 vs 蛋白名（同靶点两种写法，归一为 HGNC 符号）。

### 4. Indication —— 适应症/疾病

**判定**：疾病名或药物声称治疗的适应症。

**⚠️ 本体不分语境**：Indication 承载「疾病本体」，**无论出现在适应症列表还是不良反应列表，疾病/症状一律归 Indication**（如 `Congestive heart failure` 作为适应症或不良反应都是同一个疾病实体）。角色通过关系区分：`Drug -[TREATS]-> Indication`（适应症）vs `Drug -[CAUSES]-> Indication`（不良反应）。

**命名**：ICD/MeSH 标准名 + 分期分型（如 `NSCLC Stage IV`）。

**关键字段**：`entity_category` 区分 `disease`（流行病学报道中的独立病名）vs `indication`（药物批准/声称治疗的适应症）。

**易混淆**：
- vs TrialIndication（后者是「某试验的适应症臂」，挂 ClinicalTrial）。
- vs SafetyEvent（不良反应列表里的疾病是 Indication 本体，不是 SafetyEvent 事件）。

### 5. Pathway —— 通路

**判定**：信号/代谢/免疫通路。

**命名**：KEGG pathway ID（如 `hsa04151`）或标准通路名。

### 6. Compound —— 化合物

**判定**：中药活性成分/化学单体（生物碱/黄酮/皂苷/多糖/萜类）。

**命名**：化合物名 + CAS 号。

**易混淆**：vs Drug（Compound 是成分单体，不是成药）；vs TCMFormula（方剂 vs 单味化合物）。

### 7. TCMFormula —— 中药方剂/饮片

**判定**：中成药/经典方/提取物/单味药饮片。

**命名**：方剂名。

**易混淆**：
- vs Drug（中药归 TCMFormula，化学药归 Drug）
- vs Compound（方剂是组合，Compound 是单味成分）
- **炮制前缀**：饮片名带炮制前缀（`炒莱菔子`/`燀苦杏仁`），核心名是药材名（`莱菔子`/`苦杏仁`），消歧时需剥离前缀

### 8. ApprovalEvent —— 审批事件

**判定**：药品批准/注销/批准文号/审评等事件。

**命名**：`药品名 + 事件类型`（`盐酸吗啉胍片-注销`、`维生素C片-批准文号`）。

**关键字段**：application_type（NDA/BLA/IND）、designations（突破性疗法/优先审评）、status（获批/注销）。

**性质**：每次公告的新事件，**天然 new_entity**（step_none 正常）。

### 9. SafetyEvent —— 安全事件

**判定**：不良反应/安全警示/黑框警告/上市后监测。

**命名**：`药品名 + 事件类型`（`秋水仙碱片不良反应`、`运动员慎用警示`）。

**⚠️ 硬约束（本体 vs 事件分界）**：SafetyEvent 是「事件」实体，`entity_name` **必须是 `药品名 + 事件类型` 拼接，禁止以疾病/症状名（如 `Congestive heart failure`、`肝损伤`）作为 entity_name**。不良反应列表中的疾病/症状是「疾病本体」，一律归 Indication，通过关系连接（`Drug -[CAUSES]-> Indication`）表达不良反应。

**性质**：天然 new_entity。

**易混淆**：
- 泛称警示（`运动员慎用警示` 这种无具体药品的泛称）是噪声，不是 SafetyEvent。
- vs Indication：不良反应列表里的疾病名是 Indication（本体），不是 SafetyEvent（事件）。

### 10. RecallEvent —— 召回事件

**判定**：药品召回。

**命名**：`药品名 + 召回`。

**关键字段**：recall_class（I/II/III 级）、recall_reason、affected_batches。

**性质**：天然 new_entity。

### 11. DealEvent —— 交易事件

**判定**：融资/许可/并购/战略合作等交易。

**命名**：交易名（含金额）。

**关键字段**：deal_category、upfront_usd/milestone_usd/total_amount_usd、status。

**性质**：天然 new_entity。

### 12. Policy —— 政策

**判定**：集采/医保目录/审批改革/临床指南/价格管控/监管规范。

**命名**：政策标题。

**性质**：天然 new_entity。

### 13. ClinicalTrial —— 临床试验注册

**判定**：临床试验本身的注册记录。

**命名**：**NCT/CTR 注册号**（`NCT04256421` / `ChiCTR2000029418`），禁止用泛称。

**易混淆**：vs TrialIndication（试验本身 vs 试验的适应症臂）。

### 14. DevelopmentProject —— 研发项目/科技专项

**判定**：合作开发项目/科技重大专项等具体项目。

**命名**：项目名。

**性质**：具体项目，天然 new_entity。

### 15. DealItem —— 交易条款

**判定**：交易的具体条款（药物权益/技术平台权益），挂 DealEvent。

**命名**：权益描述。

**关系**：`DealEvent -[HAS_ITEM]-> DealItem`，`DealItem -[INVOLVES_DRUG]-> Drug`。

### 16. TrialIndication —— 试验适应症臂

**判定**：某临床试验针对某适应症的臂（含分期）。

**命名**：`注册号-适应症-分期`（如 `ChiCTR2000029418-重型COVID-19-西医治疗`）。**应锚定 ClinicalTrial 注册号，而非药品名**。

**关系**：`ClinicalTrial -[EVALUATES]-> TrialIndication`，`TrialIndication -[FOR_INDICATION]-> Indication`。

**易混淆**：vs Indication（疾病本身 vs 试验臂）；vs ClinicalTrial（试验臂 vs 试验本身）。

---

## 三、通用判定规则（跨越所有类型）

### 3.1 禁止抽取（噪声）

| 类别 | 例子 |
|---|---|
| 泛称类别名 | `临床试验`、`化疗药物`（→ 填字段，不作为实体） |
| 文献元数据 | 日期/作者/期刊/页码 |
| 文档标题 | `祖卡木制剂说明书修订安全警示` |
| 注册号/批号 | `CQZ2200024注册申请` |
| 与医药无关实体 | 化妆品/食品公司 |
| 辅料溶媒 | 葡萄糖注射液/注射用水 |

### 3.2 命名规范

- Drug → INN 通用名；Company → 完整官方名；Target → HGNC 符号；Indication → ICD/MeSH 名；ClinicalTrial → NCT/CTR 注册号
- 禁止模糊名称（「the drug」「这家公司」「上述适应症」）

### 3.3 双语规则

- Drug/Target/Indication/Pathway 必填 `name_en`：entity_name 英文 → 直接填；中文 → 翻译
- 双语别名：英文名 → aliases 填中文；中文名 → aliases 填英文

### 3.4 事件实体命名特点（领域特色）

事件/实例型实体（ApprovalEvent/SafetyEvent/RecallEvent 等）的 `entity_name` 是「药品名 + 事件类型」拼接，**寄生在药品名上**：
- 事件类型后缀（`-注销`/`-批准文号`/`不良反应`）→ 让事件天然唯一，**无需规范**
- 剂型后缀（`片`/`胶囊`）→ 药品形态变体，**需要规范**（影响 Drug 对齐 + 事件去重）

### 3.5 本体 vs 角色分界（类型判定总原则）

实体类型表达「**本体**」（它是什么东西），不表达「**角色**」（它在什么语境下）。

| 判据 | 本体（entity） | 事件/实例（event） |
|---|---|---|
| 问自己 | 「它是不是同一个东西？」 | 「它是不是一次发生？」 |
| 身份 | 稳定，多篇文档都指向它 | 一次性，每篇都是新实例 |
| 消歧 | 要跨文档对齐（消歧主对象） | 天然 new_entity，不需对齐 |
| 例子 | 疾病、药品、公司、靶点 | 审批事件、安全事件、召回事件 |

- 同一本体跨源出现，**无论语境如何都归同一类型**（疾病 → Indication，无论适应症/不良反应语境）。
- 角色通过「关系」表达（`TREATS`=治疗 / `CAUSES`=导致不良反应），不通过「类型」表达。

---

## 四、易混淆类型速查（标注/抽取最常用）

| 混淆对 | 区分要点 |
|---|---|
| Drug vs Compound | 成药 vs 中药活性成分单体 |
| Drug vs TCMFormula | 化学药/生物制品 vs 中药方剂 |
| ClinicalTrial vs TrialIndication | 试验注册 vs 试验的适应症臂（命名锚注册号-适应症-分期） |
| Indication vs TrialIndication | 疾病本身 vs 试验臂 |
| Company vs 非医药机构 | 受监管单位算 Company，律所/会计所不算（除非交易中介） |
| 事件实体 vs 泛称 | `秋水仙碱不良反应` 是 SafetyEvent，`运动员慎用警示` 是噪声 |
| SafetyEvent vs Indication | 不良反应列表里的疾病是 Indication（本体），SafetyEvent 是「药品名+事件类型」的事件，禁止疾病名作事件名 |
| 制剂 vs 活性成分 | 剂型后缀拆 CONTAINS，复方不拆 |

---

## 五、PrimeKG 权威知识图谱（已导入，实体对齐锚点）

### 5.1 概述

PrimeKG 是「精准医学知识图谱」（Chandak et al., *Scientific Data* 2023），整合 20+ 个权威生物医学资源。本系统导入了其 **4 类核心实体节点共 55,163 个**，作为消歧的**权威对照锚点**（入库标记 `canonical_source='primekg'`、`is_verified=true`）。

### 5.2 导入的实体类型（4 类）

| 本系统类型 | PrimeKG 节点类型 | 数据来源 | 数量 | 对齐锚点（`external_ids`） |
|---|---|---|---|---|
| Drug | drug | DrugBank | 7,957 | `drugbank`（7,957，全量） |
| Indication | disease | MONDO | 17,080 | `mondo`（15,813）+ `id`（1,267） |
| Target | gene/protein | NCBI Gene | 27,610 | `ncbi_gene`（27,610，全量） |
| Pathway | pathway | Reactome | 2,516 | `id`（2,516） |

### 5.3 字段结构（`canonical_data`）

**Drug**（7,957）：`name`、`primekg_source`、`drug_class`(1,268)、`modality`(1,267)、`global_status`(1,266)、`phase_us/cn/eu/jp`(≈1,265)、`cas_number`(1,261)、`innovation_type`(1,261)、`patent_expiry_us/cn`(1,258)

**Indication**（17,080）：`name`、`primekg_source`、`icd_code`(907)、`id`(907)

**Target**（27,610）：`name`、`primekg_source`、`gene_symbol`(263)、`id`(263)

**Pathway**（2,516）：`name`、`primekg_source`、`id`(1)

> 注：`primekg_source` 记录该实体的权威来源（DrugBank / MONDO / NCBI / REACTOME）；`external_ids` 里的 DrugBank ID / MONDO ID / NCBI Gene ID 是 primekg 内部的对齐锚点（同一 ID 必然同一实体）。**但这些 ID 是 primekg 整合时从权威库带来的，监管文档里不会出现——LLM 抽取实体拿不到这些 ID（抽取实体的 drugbank/mondo/ncbi_gene 均为 0），跨源对齐只能靠命名对齐（4.1 规范化 + 4.2 语义对齐）。**

### 5.4 关系：本次导入未包含边

PrimeKG 原始约有 **810 万条边、约 30 种关系类型**，但**本系统导入时只导入了实体节点，未导入关系边**（`entity_relations` 中 primekg 源记录为 0）。

主要关系类型（背景，未来可补）：

| 关系大类 | PrimeKG relation_type | 来源 |
|---|---|---|
| 药物-药物 | drug-drug（DDI） | DrugBank |
| 药物-靶点 | drug-protein（target） | DrugBank |
| 药物-疾病 | drug-disease（indication/contraindication） | DrugBank/CTD |
| 药物-副作用 | drug-effect（side effect） | SIDER |
| 疾病-蛋白 | disease-protein（association） | DisGeNET |
| 疾病-疾病 | disease-disease（本体层级） | MONDO/DOID |
| 蛋白-蛋白 | protein-protein（PPI） | STRING/BioGRID |
| 疾病-表型 | disease-phenotype / phenotype-disease | HPO / MedGen |

### 5.5 与 16 类抽取类型的关系

- PrimeKG 4 类 = 本系统「实体型」里 Drug/Indication/Target/Pathway 4 类的**权威子集**（有 verified 背书）。
- 其余 12 类（Company/Policy/ApprovalEvent/SafetyEvent/RecallEvent/ClinicalTrial/TCMFormula 等）PrimeKG 不覆盖，只能靠 LLM 抽取 + 人工审核。
- **重要现状**：`is_verified=true` 完全等价「来自 primekg 导入」；而 LLM 抽取的实体对齐到 primekg 的比例 = **0%**（原因：跨源命名变体 + `step2_exact_alias` 硬约束 + reranker 报错），即抽取实体与 primekg 权威实体目前是两套独立数据（详见课题设计缺陷诊断）。
