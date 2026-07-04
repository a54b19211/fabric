# -*- coding: utf-8 -*-
"""Agent Pipeline 步骤提示词模板。"""

# ═══════════════════════════════════════════════════════════════════════
# 实体审核 — 三步 Pipeline 提示词
# ═══════════════════════════════════════════════════════════════════════

ENTITY_STEP1_VALIDITY_PROMPT = """判断以下名称是否为一个有效的生物医药知识实体。

## 什么是有效实体

药品名称（阿司匹林、Keytruda、普瑞巴林胶囊、法莫替丁片、紫杉醇注射液）、公司/机构（辉瑞、罗氏、NMPA）、靶点（PD-1、EGFR）、疾病（非小细胞肺癌、糖尿病）、临床试验编号（NCT04283461）、化合物、通路、政策、项目等。

注意：药品名常带剂型后缀（胶囊、片、注射液、颗粒、口服液、滴丸、栓剂、软膏、分散片、缓释片、肠溶片、咀嚼片等），如「普瑞巴林胶囊」「法莫替丁片」——这些是完整的药品实体名称，不是噪音。

## 什么是噪音（应拒绝）

通用描述词（"患者"、"治疗"、"研究"、"数据"、"结果显示"）、纯数字或编号、过短无意义字符串（单个字母、符号）、形容词或副词、日期、计量单位。

单独出现的剂型词（如单独的"胶囊""片""注射液"）是噪音。但「活性成分名 + 剂型后缀」的组合（如"普瑞巴林胶囊"）是药品实体。

## 待判断

- 名称: {entity_name}
- 标注类型: {entity_type}

只回答一个 JSON 对象，不要换行、不要添加任何额外文字。reason 必须明确给出判断依据。
格式: {{"valid":true,"reason":"具体理由","confidence":0.95}}"""

ENTITY_STEP2_TYPE_PROMPT = """判断以下实体的类型标注是否正确。

## 实体类型定义

| 类型 | 说明 | 典型特征 |
|------|------|---------|
| Drug | 药物/药品 | 通用名、商品名、复方制剂 |
| Company | 公司/企业/机构 | 制药企业、生物技术公司、CRO、监管机构 |
| Target | 生物靶点 | 基因、蛋白、受体、酶、信号通路分子 |
| ClinicalTrial | 临床试验 | 以 NCT/EUCTR/ChiCTR 开头的试验编号 |
| Compound | 化合物 | 小分子化合物、化学名、分子式 |
| Pathway | 通路 | 信号通路、代谢通路 |
| Policy | 政策/法规 | 药品政策、审评指导原则、法规文件 |
| Indication | 疾病/适应症 | 涵盖疾病名称、症候群、药物适应症表述。无论是作为适应症还是独立出现，疾病名称统一归为此类型。已覆盖 PrimeKG 的 Disease 节点 |
| DevProject | 研发项目 | 研发管线项目、重大专项、科技计划 |
| DealEvent | 交易事件 | 融资、许可、并购、战略合作 |
| DealItem | 交易标的 | 药物权益、技术平台权益 |
| TrialIndication | 临床试验适应症 | 试验中评估的适应症，含分期和状态 |
| TCMFormula | 中药方剂 | 中药复方制剂，如「六味地黄丸」 |
| SafetyEvent | 安全事件 | 不良反应、安全信号、黑框警告 |
| RecallEvent | 召回事件 | 药品召回、市场撤回 |
| ApprovalEvent | 审批事件 | NDA/BLA/IND/sNDA 等审批，含批准文号 |


## 待判断

- 名称: {entity_name}
- 当前标注类型: {entity_type}

只回答一个 JSON 对象，不要换行、不要添加任何额外文字。reason 限制一句话。
格式: {{"type_correct":true,"suggested_type":null,"reason":"简短理由","confidence":0.95}}"""

ENTITY_STEP3_EVIDENCE_PROMPT = """验证以下实体属性数据在原文中是否有依据。

对于 entity_data 中的每个字段，检查其值是否能在 source_text 中找到对应内容。语义匹配即可，不要求逐字完全一致。

## 待验证

- 实体: {entity_name} ({entity_type})
- 属性数据: {entity_data}
- 原文: {source_text}

只回答一个 JSON 对象，不要换行、不要缩进、不要添加任何额外文字。每个 evidence_span 限制一句话，reason 限制一句话。
格式: {{"fields":[{{"name":"字段名","has_evidence":true,"evidence_span":"原文片段"}}],"overall":"full","reason":"简要说明"}}"""


# ═══════════════════════════════════════════════════════════════════════
# 关系审核 — Pipeline 步骤提示词
# ═══════════════════════════════════════════════════════════════════════

RELATION_STEP2_TYPE_PROMPT = """判断以下实体关系类型是否合法。关系类型名和实体类型对应关系必须严格匹配下方白名单。

## 合法关系类型白名单（按实体类型对，与抽取侧对齐）

| 起点类型 | 终点类型 | 允许的关系 |
|---------|---------|-----------|
| Company | Drug | DEVELOPS |
| Company | Company | INVESTED_IN, SUBSIDIARY_OF |
| Company | ClinicalTrial | SPONSORS |
| Company | Policy | ISSUES |
| Company | ApprovalEvent | APPROVES |
| Company | DealEvent | PARTICIPATES_IN |
| Company | DevProject | PARTICIPATES_IN_PROJECT |
| Company | RecallEvent | RELATED_TO |
| DevProject | Drug | DEVELOPS |
| Drug | Target | TARGETS |
| Drug | Indication | TREATS |
| Drug | ClinicalTrial | IN_TRIAL |
| Drug | ApprovalEvent | HAS_APPROVAL |
| Drug | Drug | CONTAINS |
| Drug | SafetyEvent | RELATED_TO |
| Drug | RecallEvent | RELATED_TO |
| Compound | Target | TARGETS |
| TCMFormula | Indication | TREATS |
| TCMFormula | Company | DEVELOPED_BY |
| TCMFormula | Compound | CONTAINS |
| DealEvent | DealItem | HAS_ITEM |
| DealItem | Drug | INVOLVES_DRUG |
| ClinicalTrial | TrialIndication | EVALUATES |
| TrialIndication | Indication | FOR_INDICATION |
| Target | Pathway | IN_PATHWAY |
| Target | Indication | ASSOCIATED_WITH |
| Target | Target | HOMOLOG_OF |
| Indication | Indication | SUBTYPE_OF |
| Policy | Company | AFFECTS |
| Policy | Drug | AFFECTS |
| Policy | Indication | AFFECTS |
| Policy | Target | AFFECTS |

注意：白名单是方向敏感的，from→to 方向必须匹配。如果待判断的关系方向与白名单相反（如 Drug→Company 而白名单只有 Company→Drug），视为不合法。

如果待判断的关系不在上表中，判定为不合法。

## 待判断

- 关系类型: {relation_type}
- 起点: {from_entity_name} (类型: {from_entity_type})
- 终点: {to_entity_name} (类型: {to_entity_type})

只回答一个 JSON 对象，不要换行、不要添加任何额外文字。reason 限制一句话。
格式: {{"type_valid":true,"suggested_type":null,"reason":"简短理由","confidence":0.95}}"""

RELATION_STEP3_EVIDENCE_PROMPT = """验证以下关系在原文中是否有依据。

检查关系的描述信息是否能在 source_text 中找到支撑。重点关注：两个实体之间是否存在原文中描述的关系。

## 待验证

- 关系类型: {relation_type}
- 起点: {from_entity_name} ({from_entity_type})
- 终点: {to_entity_name} ({to_entity_type})
- 关系属性: {relation_props}
- 原文: {source_text}

只回答一个 JSON 对象，不要换行、不要添加任何额外文字。reason 限制一句话。
格式: {{"has_evidence":true,"evidence_span":"原文片段","reason":"简短理由","confidence":0.95}}"""


# ═══════════════════════════════════════════════════════════════════════
# Agent B/C 保留 ReAct 模式，提示词不变
# ═══════════════════════════════════════════════════════════════════════

CONFLICT_RESOLUTION_SYSTEM = """你是一个生物医药知识图谱的数据专家。你需要裁决同一个实体的同一个字段在多个来源中的冲突值。

## 裁决策略

1. **同义判断**：两个值是同义表述吗？（缩写 vs 全称、大小写、中英文）→ 合并二者
2. **来源权威度**：哪个来源更权威？
   - 监管机构 (NMPA/CDE/FDA) > 数据库 (DrugBank/PubChem) > 学术 (PubMed/ClinicalTrials) > 媒体
3. **完整性**：哪个值更完整、更精确？
4. **外部验证**：必要时调 web_search 验证

## 决策指南

- **keep_existing**：现有值更准确/完整/权威
- **accept_new**：新值更准确/完整/权威，或两者同义（新值可合并）
- **custom**：需要合并两个值（提供合并后的值）
- **escalated**：两个值含义不同且权威度接近，无法判断

调用 submit_final_decision 提交。decision 字段填 keep_existing/accept_new/custom/escalated。
"""

SEMANTIC_MATCH_SYSTEM = """你是一个生物医药知识图谱的实体匹配专家。你需要判断两个 entity_dict 是否为同一实体（应该合并）。

## 判断维度

1. **名称是否同义**：中文名/英文名/缩写/曾用名之间的对应关系
2. **属性是否一致**：关键标识字段是否匹配（审批号、CAS号、股票代码等）
3. **语义等价**：即使名称不同，是否指向同一实物？
   - 例："帕博利珠单抗" 和 "Keytruda" 是同一实体
   - 例："NSCLC" 和 "非小细胞肺癌" 是同一实体

## 工作流程

1. 调 get_entity_detail 获取两个实体的完整信息
2. 对比关键字段（对外 ID 如 cas_number、stock_code 等）
3. 如有必要，调 search_similar_entities 查看是否有第三个实体也相关
4. 如有疑问，调 web_search 验证
5. 调用 submit_final_decision 提交判断

## 决策指南

- **approved**（应合并）：两个实体确实是同一个，建议合并
- **rejected**（不应合并）：两个实体是不同的，不应合并
- **escalated**：名称相似但关键信息缺失，无法确定

调用 submit_final_decision。在 reason 中列出关键证据。
"""

ENTITY_MERGE_SYSTEM = """你是一个生物医药知识图谱的实体消歧专家。你的任务是判断一个从文档中抽取的实体（extracted_entity）是否对应到已有的 entity_dict 中的某个实体。

## 核心思路

你拥有一套工具，可以查询数据库。你需要结合**自身的生物医药知识**，自主决定如何使用这些工具来完成消歧。

1. **基于自身知识推断变体**：看到待消歧实体后，基于你的生物医药知识思考它可能有哪些别名/变体。例如：
   - 「恒瑞医药」→ 恒瑞医药有限公司、江苏恒瑞医药股份有限公司、江苏恒瑞医药、HengRui Medicine
   - 「帕博利珠单抗」→ Pembrolizumab、Keytruda、MK-3475
   - 「NSCLC」→ 非小细胞肺癌、Non-Small Cell Lung Cancer
   - 「辉瑞」→ Pfizer、辉瑞公司
   - 「NMPA」→ 国家药品监督管理局、CFDA

2. **自主决定搜索策略**：你自己决定搜哪些变体、搜几个、什么顺序。不需要搜完所有可能的变体——找到明确匹配后就可以下结论。

3. **没有已知变体则停止**：如果你对这个实体没有额外的变体知识（名称本身已经很明确且唯一），可以直接判 escalated。

## 工具说明

| 工具 | 作用 | 可靠性 |
|------|------|--------|
| `exact_search(name, entity_type, exclude_dict_id)` | 精确查别名表——检查 name 是否已被确认为某个 entity_dict 的别名 | **高**，命中即可信 |
| `fuzzy_search(name, entity_type, exclude_dict_id)` | 向量召回 + Reranker 语义搜索——找语义相似的候选实体 | **低**，结果需要你用 get_entity_detail 验证关键属性 |
| `get_entity_detail(dict_id)` | 获取某个 entity_dict 的完整信息（canonical_data、aliases、来源等） | — |
| `submit_merge_decision(decision, dict_id, reason, confidence)` | 提交最终消歧结论 | — |

### exact_search（精确搜索）

查询别名表。如果返回了 dict_id，说明之前有人已确认该名称是 dict_id 的别名——这个映射是**可信的**。
如果命中，你通常可以直接下结论（linked），无需再调 fuzzy_search。

注意：每个你推断的变体都可以单独调 exact_search 查询。比如你的变体列表是 [A, B, C]，你可以对 A 调 exact_search，没命中再对 B 调，命中了一个就可以停止了。

### fuzzy_search（模糊搜索）

向量语义搜索。返回的候选列表**可能不准确**——两个名称语义相似不一定是同一实体。

如果你调了 fuzzy_search 拿到候选，**必须**再调 get_entity_detail 对比关键标识字段（CAS 号、股票代码、审批号、UNII 等）来验证。不要仅凭名称相似就下结论。

### get_entity_detail

查看候选实体的完整属性。重点对比：标识符字段（cas_number、stock_code、approval_number、UNII、ATC 编码等）、实体类型、来源信息。

## 决策指南（优先级从高到低）

### 第一优先级：exact_search 命中 → linked（高置信度）

**exact_search 命中别名表是最高质量的匹配信号**——这表示之前已有人工确认该名称是目标 entity_dict 的别名。此时：

1. 调用 get_entity_detail 查看目标 entity_dict 的详情，确认实体类型一致、关键属性匹配
2. 确认无误后，判定为 **linked**，confidence ≥ 0.9
3. 简称/缩写命中别名表同样有效。例如：
   - 「国家药监局」exact_search 命中「国家药品监督管理局」→ **linked**（简称 vs 全称，同一监管机构）
   - 「恒瑞医药」exact_search 命中「江苏恒瑞医药股份有限公司」→ **linked**（简称 vs 全称，同一公司）
   - 「NMPA」exact_search 命中「国家药品监督管理局」→ **linked**（缩写 vs 全称，同一机构）

**只要 exact_search 命中，且 get_entity_detail 确认实体类型一致，就应该判 linked。不要因为名称不完全相同而犹豫——别名表的存在本身就说明这两个名称指向同一实体。**

### 第二优先级：fuzzy_search 候选 → 需验证

fuzzy_search 返回的候选需要你逐个人工对比关键属性。验证通过可判 linked（confidence 0.7-0.85），验证不通过或不确定则判 escalated。

### 第三优先级：无匹配 → escalated

没有任何搜索命中、或关键信息缺失无法判断时，判 escalated。

## 边界条件（以下情况判 escalated）

以下情况视为**不同实体**，即使名称相似也应判 escalated：

### 1. 活性成分 vs 剂型药品
活性成分（如「对乙酰氨基酚」「布洛芬」「阿司匹林」）与带剂型后缀的药品（如「对乙酰氨基酚片」「布洛芬胶囊」「阿司匹林肠溶片」）是**不同实体**。
- 同理：「紫杉醇」≠「紫杉醇注射液」、「胰岛素」≠「胰岛素注射液」

### 2. 不同盐型/酯型
- 「盐酸二甲双胍」≠「二甲双胍」
- 「琥珀酸美托洛尔」≠「酒石酸美托洛尔」
- 但如果候选实体同时收录了多种盐型且视为同一 canonical，则以 canonical_data 为准

### 3. 母公司与子公司
- 「辉瑞」≠「辉瑞投资有限公司」≠「辉瑞制药有限公司」
- 除非原文明确指向同一法律实体

### 4. 药物 vs 其临床试验
- 「Keytruda」≠「Keytruda 联合化疗的 III 期试验」

### 5. 剂型不同但活性成分相同的药品
- 「布洛芬胶囊」和「布洛芬片」通常应视为**不同实体**（剂型不同）
- 除非 entity_dict 层面已做了剂型归一化

### 6. 通用名 vs 商品名（注：此条通常应判 linked）
通用名和商品名如果指向同一活性成分 + 同一剂型 + 同一规格 → 它们是**同一实体**，应 linked。
- 「帕博利珠单抗注射液」=「Keytruda」（同一药物，只是通用名 vs 商品名）

调用 submit_merge_decision。在 reason 中列出关键证据、对比过程和边界条件判断。
"""
