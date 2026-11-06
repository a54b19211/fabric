# Data-Fabric 架构与开发位置

更新日期：2026-11-06（integration/rebuild-2026-11：重构线与 feat/server_fix 合并后）。部署状态以 README 的“版本使用”为准。

## 1. 这个项目负责什么

把网上的资料保存下来，保留来源和版本；把可靠的结构化字段交给图谱和雷达。正文可以先交给雷达，不用等所有对象和关系审核完。

“原文这样写”“这个名称指的是谁”“这个结论在医学上是否成立”是三个问题。Fabric 负责前两个问题的数据和审核记录；专业证据评价由汪鑫怡的服务负责，最终文字核验由潘倩的服务负责，调查调度和研判在雷达。

## 2. 一份资料经过哪些地方

~~~text
采集框架（来源目录 → 读取插件，每次传输过闸计量）
    ↓
固定版本（History：原件、投影、来源给出的材料性质）
    ↓
加工层（附件解析、清洗正文、编号、画像、登记差分；处理器带版本）
    ↓
正文索引发布 ───────────────────→ 雷达读原文、WeKnora 建索引
    ↓
有可靠原生字段：按规则读取
只有自然语言：模型提取候选
    ↓
对象消歧 → 明确编号可链接；名称和相似度只提建议
    ↓
人工确认对象及关系，保留审核人与固定原文
    ↓
图版本发布 ─────────────────────→ 雷达查询关联、回读依据
~~~

失败必须显示失败；“没有新资料”“资料没读到”“写入失败”不能共用一个成功结果。

## 3. 改代码到哪里找

| 位置 | 负责的工作 |
|---|---|
| `collection/catalog.py` | 唯一的来源目录：每个来源的插件、限速组、增量键、覆盖层级 |
| `collection/`（`transport.py`、`rate.py`、`windows.py`、`site_crawler.py`） | 读取闸门与预算、共享限速、增量窗口、站点插件 |
| `config/sites/` | 每个站点的入口、列表和正文规则（站点插件读取） |
| `biz_rules/history_sources.py`、`biz_rules/history_models.py` | 读取插件注册表、原件投影、History 契约 |
| `biz_rules/document_evidence.py` | 材料类型、全文、更正指向、同源元数据 |
| `biz_rules/gdelt_news.py`、`who_don.py`、`sec_xbrl.py` | 各公开接口来源的读取与投影 |
| `processing/` | 附件解析、清洗正文、编号、画像、登记差分、索引分片 |
| `db/history.py` | 固定版本、历史读取 |
| `db/publication.py` | 对象库到 History 的持续发布 |
| `db/clean_stage.py`、`db/derived_text.py`、`db/extraction_checks.py` | 加工层的任务队列与派生表 |
| `db/index_publication.py` | 向 WeKnora 发布固定版本并核对回执（文档正文进 WeKnora 的唯一路径） |
| `flows/extraction_flow.py`、`biz_rules/native_extraction.py` | 结构化来源分流、模型提取、候选处理；ClinicalTrials/FDA 已确认结构的字段映射 |
| `disambiguation/` | 同名对象的候选查询与比较、名称规范化规则 |
| `db/identity.py`、`db/repository.py`、`api/review_auth.py` | 来源证据、身份提案、人工确认与撤销 |
| `flows/graph_publish.py`、`biz_rules/native_graph.py`、`biz_rules/reviewed_graph.py` | 有范围、有原文依据的图资料发布 |
| `flows/history_fetch.py` | 公开读取与隔离模拟补采的 RQ 作业 |
| `core_engine/authority_*.py`、`biz_rules/drug_name_normalizer.py` | 药名与标识符对齐库（PubChem/ChEMBL/本地格式校验）；调用入口见 §8 |
| `contracts/` | 数据契约与业务验收样例 |
| `api/`、`frontend/` | HTTP 接口、人工操作页面 |

采集只有一条路：来源在 `collection/catalog.py` 声明，读取插件在 `biz_rules/history_sources.PLUGINS` 注册。旧 ETL（`main_etl.py`、`flows/crawl_flow.py`、存储写入与质检链）已删除，不要再复制一份爬虫或写库逻辑。

## 4. 对象什么时候算确认了

原生注册号必须同时满足：来源结构正确、编号命名空间明确、对象类型相符、没有多个冲突命中。存储前还会对照已经落盘的原件重新核对。模型返回一个 `_native` 字段不能获得这种资格。

名称相同、别名相同、向量分高、模型认为相同，都只产生候选。没有完成的人工标注集，当前阈值没有校准，不能声称某个分数对应某个准确率。

`review_status=approved` 只是内容审核状态。确认身份还要有这条记录的审核人、时间、依据，字典的审核记录，以及当次固定原文。旧的 `auto_approved` 和 `is_verified` 原样保留，但不能自动补成人工结论。重复出现只增加来源证据。

接受单条链接提案，只改变这一条记录。整字典合并走明确的合并操作，记录受影响对象并支持撤销；目标在提案后发生变化时，先重新核对，不能把旧提案直接用于新对象。修改对象字段后，原确认不能继续替修改后的内容背书。

## 5. 原文和审核依据怎样保存

- `entity_source_evidence` 追加保存不同来源，同一材料重放不重复增加。
- API 的 `evidence` 返回全部来源；`current_review_evidence` 返回与当前审核相符的那次依据。新增一份资料不能借用旧审核自动变成已确认。
- 原件有固定 `version_id` 和原文位置。字符位置按 Unicode 计数；原 Markdown 包在 JSON 里时，还要先取 `container_json_pointer` 指定的字段。
- 来源给出的材料性质（`document_evidence`）随新版本保存，但不参与版本身份：它由原件和投影算出，不能区分两个版本。合并前首次见到的版本没有这一项。
- 文档发布时间、采集时间、对象有效时间分开。未知时间留空，不能借文档日期填上对象的出生或失效时间。
- 文档更正、撤回、补充、转载通过 `document_relations` 指向原记录。旧版继续保留。

## 6. 图给下游什么

图发布只包含本次明确选定的文档和对象，不代表全图。原生字段图保留规则版本和字段位置；人工图保留对象确认、关系审核及原文出处。第二份文档里的佐证也能被查到，不需要复制对象。

`reviewed` 查询只交付符合确认条件的对象和关系。`with_candidates` 可以查看未确认对象，状态保留，不能当作已成立的事实。图里缺少某个关系，不代表现实中没有关系。

PrimeKG 当前内容只作背景；映射先作为提案，不能根据同名自动确认。当前图和新别名不会倒填到十年前。新版读取兼容已发表的旧版图规则，新发布继续采用当前严格规则。

## 7. 运行和存储

沿用 FastAPI、SQLAlchemy/PostgreSQL、Redis/RQ 和 Vue，不新建工作流平台或第二套图数据库。

原对象库与独立 History 库通过固定输入和持久待办接续。对象库提交成功以后才发布；跨服务结果未知时先核对原请求，不盲目再发一次。WeKnora 只有确认相应版本可检索后才算完成，真实资料和演示资料分开。

文档正文只经 `db/index_publication` 和桥接服务进 WeKnora；`/api/weknora` 只推审核图。查询和重试用 History 的索引发布接口。

## 8. 目前还缺什么

公开接口包括 ClinicalTrials、PubMed、FDA CRL、Europe PMC、WHO DON、SEC 和 GDELT。GDELT 当前只接新闻索引，实际探测遇到过限速；不能当成已经稳定得到新闻全文。各级卫健委还要逐站核查，ProMED 尚缺许可，实时社媒平台和供应商未确定。

药名与标识符对齐库（`core_engine/authority_*`、`biz_rules/drug_name_normalizer.py`、表 `doc_authority_resolution`）原来由旧 ETL 的写库步骤调用；旧 ETL 删除后它还没有接到采集框架或加工层上，见 `docs/decisions/0001-merge-server-fix.md`。

这次对旧自动审核记录做了只读统计与抽样，没有给样本贴“正确/错误”标签，也没有批量重写旧状态。接下来应由徐子雯补来源验收，唐蔚雯组织身份样本标注；专业人员再判断哪些规则和阈值可扩大使用。

具体测试、迁移、交接和回退见 README 与 `task_doc/数据采集与图谱修改报告.md`。
