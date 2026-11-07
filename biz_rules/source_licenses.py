"""Licence and legal declarations of the sources Fabric reads: one declaration per source, the only place the text lives.

Three groups, validated against each other when this module is imported (a mismatch is an ImportError-level failure of
the service, not a runtime surprise):
- COLLECTED: the sources the collection catalog (collection.catalog) can fetch from - one LicenseSpec per fetchable
  CollectionSpec, hand-declared for the API sources and the local-file import, generated from the collection spec for
  the site plugins (they share one licence position: each needs an administrator's explicit allowance);
- DERIVED: sources that are not collected but derived inside Fabric or imported under control (reviewed graph, PrimeKG
  background, the controlled archive of fixed originals);
- RETIRED: biz_rules.retired_sources, never offered for new selection.

SOURCE_CATALOG (biz_rules.history_sources) is generated from these. The History library's history_sources table keeps
the administrator's decisions (read_enabled, dependencies, published_seq); the descriptive text is these declarations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class LicenseSpec:
    source_id: str
    name: str
    business_types: tuple[str, ...]
    acquisition_channel: str
    access: str
    license_url: str | None
    history_coverage: str
    native_id: str
    read_scope: str
    limitations: tuple[str, ...]
    attribution: str | None = None
    endpoint_kind: str | None = None
    native_analysis_mode: str | None = None
    # derived sources: the sources whose permission this source depends on (set by the administrator in the library)
    dependency_source_ids: tuple[str, ...] | None = None
    # the host's licence decision: academic, non-commercial, no redistribution
    academic_use_only: bool = True
    # original: fixed originals of a source; graph: a reviewed graph package (read through the graph interface)
    record_kind: Literal["original", "graph"] = "original"

    def catalog_entry(self) -> dict:
        """The entry of SOURCE_CATALOG (the shape /api/history/sources has always published)."""
        entry = {
            "name": self.name,
            "business_types": list(self.business_types),
            "acquisition_channel": self.acquisition_channel,
            "access": self.access,
            "license_url": self.license_url,
        }
        for key in ("attribution", "endpoint_kind", "native_analysis_mode"):
            if getattr(self, key) is not None:
                entry[key] = getattr(self, key)
        entry.update(history_coverage=self.history_coverage, native_id=self.native_id, read_scope=self.read_scope)
        if self.dependency_source_ids is not None:
            entry["dependency_source_ids"] = list(self.dependency_source_ids)
        entry["limitations"] = list(self.limitations)
        return entry


COLLECTED = (
    LicenseSpec(
        source_id="gdelt_doc",
        name="GDELT DOC 新闻索引",
        business_types=(
            "population_need",
            "corporate_finance",
            "regulatory",
            "literature",
        ),
        acquisition_channel="public_api",
        access="public_metadata",
        license_url="https://www.gdeltproject.org/about.html#termsofuse",
        attribution="The GDELT Project — https://www.gdeltproject.org/",
        native_analysis_mode="news_index_not_independent_evidence",
        history_coverage="仅本地采用后版本；实时DOC接口不是十年新闻档案",
        native_id="完整新闻URL的SHA256，query参数保留",
        read_scope="查询的有界新闻索引；默认最近24小时，支持显式起止窗口",
        limitations=(
            "只有标题、原网址和索引元数据，没有新闻全文；第三方正文另核许可",
            "GDELT seendate是索引观察时间，不是新闻发布时间或本地首次见到时间",
            "没有连续分页；达到上限需宿主缩小时间窗，不能推断已覆盖所有报道",
            "转载和共同源尚未核实，不能按网址数量计算独立证据数",
        ),
    ),
    LicenseSpec(
        source_id="sec_companyfacts",
        name="SEC XBRL 公司概念事实",
        business_types=(
            "corporate_finance",
            "research_capacity",
        ),
        acquisition_channel="public_api",
        access="public_structured_filings",
        license_url="https://www.sec.gov/search-filings/edgar-application-programming-interfaces",
        endpoint_kind="companyconcept",
        native_analysis_mode="company_concept_reported_facts",
        history_coverage="本地采用后版本；当前多期披露集合不是过去API快照",
        native_id="CIK十位数:taxonomy:tag；每个原件为官方单概念完整JSON",
        read_scope="明确CIK/概念清单；按单位保留所有披露、期间、filed及accession，不选最新行",
        limitations=(
            "窄CompanyConcept端点属于SEC CompanyFacts同族；不是整公司CompanyFacts原件切片",
            "fy/fp是申报财政期间，不替代每个事实的start/end；filed不是本地first_seen",
            "单位、币种及重复披露分别保留；未提供合并范围/分母时未知，不编造汇率或可比性",
        ),
    ),
    LicenseSpec(
        source_id="clinicaltrials",
        name="ClinicalTrials.gov",
        business_types=("trials_pipeline",),
        acquisition_channel="public_api",
        access="public_registry",
        license_url="https://clinicaltrials.gov/data-api/about-api",
        native_analysis_mode="registered_study_not_drug_program",
        history_coverage="本应用采用后不可变版本；当前API不是过去快照",
        native_id="NCT ID",
        read_scope="公开登记字段，含未决/终止记录",
        limitations=("登记状态不是药物成败；日期保留原精度和ACTUAL/ESTIMATED",),
    ),
    LicenseSpec(
        source_id="pubmed",
        name="PubMed",
        business_types=("literature",),
        acquisition_channel="public_api",
        access="public_metadata",
        license_url="https://www.ncbi.nlm.nih.gov/home/about/policies/",
        history_coverage="本应用采用后版本；无匿名十年完整快照保证",
        native_id="PMID",
        read_scope="EFetch元数据、摘要和更正关系；全文只保存许可明确的（PMC JATS声明CC/公共领域许可、Unpaywall许可明确的OA PDF），各成附件版本；许可不明或不可取得的记缺失",
        limitations=(
            "同PMID/DOI跨库不构成独立研究证据",
            "全文许可按原件<license>或Unpaywall声明判定，未经人工核实；Unpaywall需配置联系邮箱",
        ),
    ),
    LicenseSpec(
        source_id="fda_crl",
        name="FDA Complete Response Letters",
        business_types=("regulatory",),
        acquisition_channel="public_api",
        access="public_metadata",
        license_url="https://open.fda.gov/license/",
        history_coverage="当前公开记录的小批采用；信函日期不证明当时可得",
        native_id="file_name + letter_date/type/application_number 复合键",
        read_scope="公开信函文本及原生字段；不把所有letter_type当否决",
        limitations=("同PDF可有多轮信；复合键歧义须拒绝，不猜项目谱系",),
    ),
    LicenseSpec(
        source_id="europe_pmc",
        name="Europe PMC",
        business_types=("literature",),
        acquisition_channel="public_api",
        access="public_metadata",
        license_url="https://europepmc.org/Copyright",
        native_analysis_mode="bibliographic_record_not_independent_study",
        history_coverage="当前元数据/更正关系采用；非过去全文版本档案",
        native_id="source:id",
        read_scope="core元数据/摘要/许可和关系；不自动抓全文",
        limitations=(
            "firstPublicationDate可能补算，保留原值，不作历史原件证明",
            "PMID/DOI跨库复用不是新独立研究；正文另核许可",
        ),
    ),
    LicenseSpec(
        source_id="who_don",
        name="WHO Disease Outbreak News",
        business_types=("population_need",),
        acquisition_channel="public_api",
        access="public_metadata",
        license_url="https://www.who.int/about/policies/publishing/copyright",
        native_analysis_mode="notice_event_only",
        history_coverage="本地采用后版本；当前公告API不提供过去原件资格",
        native_id="WHO原生Id（DonId单独保留）",
        read_scope="明确LastModified时间窗内DON公告、原文和发布元数据",
        limitations=(
            "DON是选择性公告，不是全球疫情全量；零公告不证明没有暴发",
            "发布/更新日期不是暴发开始；不从叙述推病例数、发病率或专业确认",
            "本API无结构化人群分母，仅供公告事件模式；不能生成发病率/病例数序列",
        ),
    ),
    LicenseSpec(
        source_id="local_files",
        name="本地文件原件导入（PDF / Office）",
        business_types=(
            "literature",
            "regulatory",
            "trials_pipeline",
        ),
        acquisition_channel="institutional_upload",
        access="controlled_import",
        license_url=None,
        history_coverage="上传/导入后的不可变版本；文件日期不是来源发布日期",
        native_id="local-file:<集合>/<文件名>；版本按原件sha256固定",
        read_scope="管理员上传或服务器收件目录中的单个原件；正文由加工层附件解析产出（页/块/表定位）",
        limitations=(
            "许可与来源由上传者负责，系统不核实",
            "集合名和文件名由上传者给出，不是来源原生编号",
            "不推断发布日期、作者或专业结论",
        ),
    ),
)

DERIVED = (
    LicenseSpec(
        source_id="reviewed_graph",
        name="唐的有界审核图发布",
        business_types=(
            "mechanisms",
            "trials_pipeline",
        ),
        acquisition_channel="derived_export",
        access="controlled_import",
        license_url=None,
        history_coverage="发布后版本；不回填当前别名/合并到过去",
        native_id="明确选择范围及批次；包内namespace+源实体/关系ID",
        read_scope="所选发布子图；图审核不等于专业证据评价",
        dependency_source_ids=(),
        limitations=("不是全图完整邻域；任何原来源撤权保守封整个派生来源组",),
        record_kind="graph",
    ),
    LicenseSpec(
        source_id="primekg_graph",
        name="PrimeKG 当前有界背景",
        business_types=("mechanisms",),
        acquisition_channel="derived_export",
        access="background_context",
        license_url=None,
        history_coverage="本地first_seen后可回看已收到的背景；无外部过去档案或文档证据",
        native_id="原生namespace+ID及有向关系",
        read_scope="管理员明确获准的当前有界捕获，不自动联网或扩展全图",
        dependency_source_ids=(),
        limitations=("不当作新独立研究、机制核验或过去时点事实；许可另核",),
        record_kind="graph",
    ),
    LicenseSpec(
        source_id="external_archive",
        name="受控历史归档",
        business_types=(
            "trials_pipeline",
            "regulatory",
            "literature",
            "corporate_finance",
            "population_need",
        ),
        acquisition_channel="institutional_upload",
        access="controlled_import",
        license_url=None,
        history_coverage="按每个明确声明/审核包记录，不承诺现有十年档案",
        native_id="归档原站ID；demo和真实资料隔离",
        read_scope="固定trial/program/outcome/document原件及同口径metric/event声明/字段绑定；不提供专业真实性判断",
        limitations=(
            "普通导入未审核；不能倒填本地首次见到时间",
            "映射只接纳有出处审核，不做唐蔚雯负责的消歧",
        ),
        record_kind="original",
    ),
)


def site_license(item) -> LicenseSpec:
    """The licence position every site plugin shares; generated from its collection spec."""
    return LicenseSpec(
        source_id=item.source_id,
        name=item.name,
        business_types=tuple(item.evidence_types),
        acquisition_channel="site_plugin",
        access="explicit_source_allowlist",
        license_url=None,
        history_coverage="本系统采用后的不可变版本；不是原站历史档案",
        native_id="详情页 URL；接口记录/表格为 uuid5(站点:记录键)；附件为 父记录#attachment=链接",
        read_scope="按需读取或按列表入口续页；documents 库存按原站经 legacy_documents 冻结",
        limitations=("需管理员逐源许可", "插件未对原站实测（verified=False）", "不保证原站覆盖、原日期语义或专业结论"),
    )


def collected_licenses() -> dict[str, LicenseSpec]:
    """One LicenseSpec per fetchable collection spec: the hand-declared ones, then the site plugins by id."""
    from collection.catalog import SITE_SOURCES, spec

    licenses = {item.source_id: item for item in COLLECTED}
    for source_id in sorted(SITE_SOURCES):
        licenses.setdefault(source_id, site_license(spec(source_id)))
    return licenses


def validate_declarations(collected: dict[str, LicenseSpec] | None = None) -> None:
    """Cross-validate collection specs, licence declarations and the retired list. Raises ValueError listing every
    mismatch: a fetchable source without a licence, a licence for a source nothing can fetch, a source in two
    groups, a graph source marked as a collected one."""
    from biz_rules.retired_sources import RETIRED
    from collection.catalog import FETCHABLE

    collected = collected_licenses() if collected is None else collected
    derived = {item.source_id: item for item in DERIVED}
    retired = set(RETIRED)
    errors = []
    errors += [
        f"{s}: fetchable in the collection catalog but has no LicenseSpec" for s in sorted(FETCHABLE - set(collected))
    ]
    errors += [
        f"{s}: has a LicenseSpec in COLLECTED but the collection catalog cannot fetch from it"
        for s in sorted(set(collected) - FETCHABLE)
    ]
    for first, second, label in (
        (set(collected), set(derived), "collected and derived"),
        (set(collected), retired, "collected and retired"),
        (set(derived), retired, "derived and retired"),
    ):
        errors += [f"{s}: declared as {label}" for s in sorted(first & second)]
    if len(derived) != len(DERIVED) or len(COLLECTED) != len({c.source_id for c in COLLECTED}):
        errors.append("a source is declared twice within a group")
    errors += [
        f"{s}: a collected source cannot be a graph source"
        for s, item in collected.items()
        if item.record_kind != "original"
    ]
    if errors:
        raise ValueError("source declarations disagree: " + "; ".join(errors))


def source_catalog() -> dict[str, dict]:
    """SOURCE_CATALOG: collected API sources, the retired bridge (until it leaves the catalog), derived sources, then
    the site plugins - the order the catalog has always had."""
    from biz_rules.retired_sources import RETIRED

    collected = collected_licenses()
    validate_declarations(collected)
    api = [item for item in COLLECTED]
    sites = [item for source_id, item in collected.items() if source_id not in {c.source_id for c in COLLECTED}]
    catalog = {item.source_id: item.catalog_entry() for item in api}
    catalog.update({source_id: retired.catalog_entry() for source_id, retired in RETIRED.items()})
    catalog.update({item.source_id: item.catalog_entry() for item in DERIVED})
    catalog.update({item.source_id: item.catalog_entry() for item in sites})
    return catalog
