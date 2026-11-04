"""v3 业务验收契约：把“雷达做对了没有”写成固定、可机检的案例，而不是靠读报告的感觉。

为什么需要它：
- v3 第一部分 1.2 给了判断 A–E 各两个验收案例，第四部分给了每条纵向链路的正例/更正/异常/判断E，
  6.4 给了国家/省级/高校三种使用视角。这些只写在文档里时，最容易被“文风像内参”“又加了几个站点”替代。
- 这里每个案例都绑定 v3 原文出处（design_ref），每个出处恰好对应一个案例；
  案例必须有反例（counterexample），必须有可机检的断言（checks），禁止的捷径写进 must_not。
- 证据类型只用 v3 1.4 的 12 种（英文 slug），来源编号只用 KNOWN_SOURCES，覆盖缺口必须如实列出。
- 夹具不编造真实世界事实：没找到的真实材料标 to_be_sourced；合成资料只验证流程（6.4），
  不能据此宣布覆盖了真实场景。

本模块只定义契约和校验，不跑雷达；以后雷达的报告/运行记录按 checks 逐条判定。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONTRACT_VERSION = "bioradar.acceptance.v1"

# ---------- 词表 ----------

# v3 1.4 的 12 种证据类型，顺序与原文一致。用英文 slug 是为了让程序和夹具不受中文写法差异影响，
# 中文名留着给报告和人读。
EVIDENCE_TYPES: dict[str, str] = {
    "registry": "登记",
    "regulatory_decision": "监管决定",
    "primary_research": "原始研究",
    "preprint": "预印本",
    "conference": "会议",
    "company_disclosure": "公司披露",
    "financial": "财务",
    "policy": "政策",
    "official_notice": "官方通报",
    "media": "媒体",
    "social": "社媒",
    "patent": "专利",
}

Coverage = Literal["available", "legacy_unverified", "legacy_stock", "gap"]


class SourceInfo(BaseModel):
    """来源目录里的一项。coverage 分开写“已采通 / 旧插件未实测 / 旧库存量 / 缺口”，
    是因为 v3 1.4 要求覆盖现状如实标注，不能把未实测的旧插件说成已采通。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    coverage: Coverage
    # True 表示编号是本模块按 v3 原文新加的（不在任务给定的编号表里），方便审阅时逐个核对
    introduced_here: bool = False
    note: str = ""


def _src(name: str, coverage: Coverage, note: str = "", new: bool = False) -> SourceInfo:
    return SourceInfo(name=name, coverage=coverage, note=note, introduced_here=new)


KNOWN_SOURCES: dict[str, SourceInfo] = {
    # 任务给定：已有
    "clinicaltrials": _src("ClinicalTrials.gov", "available", "v3：三期子集，首批改全量增量"),
    "europe_pmc": _src("Europe PMC", "available", "v3：6 组关键词子集，正改为全量增量"),
    "pubmed": _src("PubMed", "available", "v3：旧配置"),
    "who_don": _src("WHO Disease Outbreak News", "available", "v3：只有最近 30 天"),
    "sec_companyfacts": _src("SEC companyfacts", "available", "v3 原文是“白名单允许，未采”"),
    "fda_crl": _src("FDA 完整回复函", "available"),
    # 任务给定：旧站插件 legacy_site_crawler，v3 3.3a 说明本系统未对原站实测（verified=False）
    "nmpa": _src("NMPA", "legacy_unverified", "旧库存量 + 未实测插件"),
    "cde": _src("CDE", "legacy_unverified", "旧库存量 + 未实测插件"),
    "chictr": _src("ChiCTR", "legacy_unverified", "旧库存量 + 未实测插件"),
    # 任务给定：缺口
    "cde_ctr": _src("CDE 药物临床试验登记平台（CTR）", "gap", "与 ChiCTR 是两个平台"),
    "cninfo": _src("巨潮/沪深北交易所公告与财报", "gap"),
    "hkex": _src("港交所披露", "gap", "v3 判断C 写作“已有（旧配置）”，任务表按缺口处理"),
    "biorxiv": _src("bioRxiv", "gap"),
    "drugs_fda": _src("Drugs@FDA 批准数据", "gap"),
    # 本模块按 v3 原文新加
    "openfda": _src("openFDA", "legacy_stock", "v3 判断A：openFDA（旧库存量）", new=True),
    "ema": _src("EMA", "gap", "v3 判断A", new=True),
    "company_ir": _src("企业 IR 管线页和财报演示", "gap", "v3 判断A；不是独立无偏的验证来源", new=True),
    "conference_abstracts": _src("会议摘要", "gap", "v3 判断A、B", new=True),
    "patents": _src("专利", "gap", "v3 判断A；6.4 高校视角", new=True),
    "medrxiv": _src("medRxiv", "gap", "v3 判断B", new=True),
    "arxiv": _src("arXiv 相关类别", "gap", "v3 判断B：AI 制药等不限于 q-bio", new=True),
    "guidelines": _src("指南", "gap", "v3 判断B", new=True),
    "sec_filings": _src("SEC filing 原文与交易披露", "gap", "v3 判断C", new=True),
    "nhsa": _src("国家医保局", "gap", "v3 判断C", new=True),
    "procurement_prices": _src("采购与价格资料", "gap", "v3 判断C", new=True),
    "industry_media": _src("行业媒体", "gap", "v3 判断C", new=True),
    "china_cdc": _src("中国疾控", "gap", "v3 判断D", new=True),
    "health_commissions": _src("国家和地方卫健委通报", "gap", "v3 判断D", new=True),
    "ecdc": _src("ECDC", "gap", "v3 判断D", new=True),
    "us_cdc": _src("US CDC", "gap", "v3 判断D", new=True),
    "promed": _src("ProMED", "gap", "v3 判断D", new=True),
    "news_media": _src("媒体线索", "gap", "v3 判断D", new=True),
    "social_media": _src("社媒线索", "gap", "v3 判断D、1.4", new=True),
    "national_supply_stats": _src("全国统一口径供需/产能/采购/贸易数据", "gap", "v3 6.4 国家层", new=True),
    "policy_execution_data": _src("政策执行和结果数据", "gap", "v3 6.4 国家层", new=True),
    "local_industry_ledger": _src("辖区企业/园区/产线底册（授权内部台账）", "gap", "v3 6.4 省级", new=True),
    "research_institution_ledger": _src(
        "项目/经费/设备/队伍/成果授权台账", "gap", "v3 6.4 高校/科研机构", new=True),
    "grants": _src("公开的基金资助信息", "gap", "v3 6.4 高校/科研机构", new=True),
}

# 断言种类：以后判定一份报告或一次运行是否合格时，每种都要有确定的判定程序。
# 只收 v3 点名的要求，故意不收“文风像内参”之类的主观项（6.4：评价不看文风）。
CHECK_KINDS: dict[str, str] = {
    # 1.3 与第四部分正例：每份报告都要能回指原文、如实写缺口
    "cites_fixed_version": "每条依据回指固定版本：源版本、加工版本、定位、片段哈希（6.1 复现引用）",
    "states_limitation": "写明局限，包括口径、覆盖和方法的局限",
    "states_next_evidence": "写明下一条会改变判断的证据是什么",
    "gap_stated": "没接入或没解析出来的来源写成缺口，不写成“已覆盖”或“不存在”",
    # 判断 A
    "detects_change": "由新旧版本的确定性差分产出观测（如登记状态改变、终点修改），并进入专题",
    "no_efficacy_from_status": "不用“到了几期”或 COMPLETED 等登记状态推出疗效或失败",
    "no_hype_overestimate": "宣传热度、新闻数量不抬高证据成熟度和跟进建议",
    "evidence_maturity_compared": "同口径比较证据成熟度和差异化（人群、终点、对照、样本量）",
    "uses_graph_lookup": "确实需要查图谱：身份/别名或竞品关联（同靶点、同适应症）",
    # 判断 B
    "evidence_level_stated": "区分提出主张、数据进步、证据升级、可能改变决策四个层次并给出证据等级",
    "replication_counted": "统计独立重复验证；同源材料不算独立重复",
    "retraction_tracked": "更正和撤稿关系追踪到受影响的主张",
    "uses_mech_evaluation": "确实需要机制评价（汪的 MechEvidence），按命题+适用范围+材料版本评价",
    "no_volume_proxy": "材料、公告、论文、转发的数量只当信息活动指标，不当实质指标",
    # 判断 C
    "measurement_has_caliber": "每个测量值带报告期、单位、范围、分母；分母不知道写 unknown，不补",
    "contract_not_revenue": "合同额/订单额与实际收入分开，不互相替代",
    "change_point_reported": "口径对齐后给出指标序列和变点",
    # 判断 D
    "visibility_cutoff_respected": "回放只用截止时间前可见的版本，可见性按 Fabric 的回放模式判定",
    "same_origin_not_independent": "转发、转述、同源链接不算多份独立证实",
    "competing_explanations_listed": "并列竞争性解释，不只给一个结论",
    "verification_priority_stated": "给出核实优先级",
    "claim_existence_not_truth": "区分“这条线索/主张确实出现过”和“主张内容成立”",
    "baseline_compared": "与基线比较，而不是只看绝对数",
    "timeliness_evaluated": "误报、漏报、核实延迟一起评价",
    # 判断 E 与更正
    "recompute_only_affected": "更正或撤权只重算受影响的事件、时期、指标和命题，其余不动",
    "keeps_old_judgment": "旧判断版本保留可查，不被覆盖",
    "revision_reason_stated": "修订说明写清判断为什么变（或为什么维持）以及依据的固定版本",
    "follow_up_supported": "修订后的判断可以接着追问",
    "permission_enforced": "撤权后已存的报告、摘要、缓存、快照也不再泄露受限内容（6.1）",
    # 第四部分 异常
    "no_duplicate_on_restart": "重启后不重复落盘、不重复产出观测",
    "resumes_after_partial_failure": "做到一半失败能从检查点接着做，欠账可见",
    "no_derived_feedback_loop": "图谱、报告、计算结果不再进入抽取，加工产物不形成回路",
    "identifier_not_double_counted": "同一 PMID、NCT 等编号（或同一公司+报告期+指标）不重复计数",
    # 6.4 使用视角
    "answers_pending_question": "回答了待决问题（含期限）",
    "key_evidence_not_missed": "关键证据没有漏",
    "counter_evidence_changes_judgment": "反证确实能改变判断，而不是列出后不理",
    "updatable_with_new_evidence": "后续出现新证据时能更新判断",
    "analysis_separate_from_recommendation": "分析判断和对策建议分开写，价值取舍由任务方给出",
    "no_causal_from_before_after": "只有前后差异时只报告“描述性关联”，不下因果结论",
    "no_precise_figure_without_denominator": "没有分母时不报精确的依赖度或比例",
    "comparison_controls_caliber": "区域之间、机构之间比较时控制口径、规模和时间",
    "no_real_coverage_claim": "合成资料只验证流程，不据此宣布已覆盖真实场景",
}

# 6.4 的五条评价标准（不看文风）
VIEW_EVALUATION_CHECKS: tuple[str, ...] = (
    "answers_pending_question",
    "key_evidence_not_missed",
    "counter_evidence_changes_judgment",
    "gap_stated",  # “选项的前提和数据缺口是否如实写明”
    "updatable_with_new_evidence",
)

# v3 1.3：每份报告必须说清
REPORT_MUST_SAY: tuple[str, ...] = (
    "主要判断", "业务影响", "支持和反对的证据", "局限", "下一条会改变判断的证据",
)

# v3 1.2 各判断“报告”一行点名的内容
JUDGMENT_MUST_SAY: dict[str, tuple[str, ...]] = {
    "A": ("跟进建议和理由", "证据成熟度", "差异化", "失败原因", "下一个里程碑"),
    "B": ("突破判断", "证据等级", "反证"),
    "C": ("赛道判断", "关键指标序列", "变点", "口径局限"),
    "D": ("威胁判断", "核实优先级"),
    "E": ("修订说明", "判断为什么变", "旧判断保留", "可以追问"),
    "view": (),
}

# v3 6.4 内参正文结构，只要求使用视角案例
BRIEFING_STRUCTURE: tuple[str, ...] = (
    "待决问题与期限",
    "主要判断及其影响",
    "支持和反对的证据以及证据缺口",
    "情景或可选行动及其代价、前提和风险",
    "需补充的关键资料与重新复核条件",
)

# v3 点名禁止的捷径；同一判断的每个案例都必须把它们写进 must_not
REQUIRED_MUST_NOT: dict[str, tuple[str, ...]] = {
    "A": ("用“到了几期”或“COMPLETED”代替疗效",),
    "B": ("按新闻热度或论文数量认定突破",),
    "C": ("公告变多等于产业增长", "合同额当作收入"),
    "D": ("广泛转发算成多份独立证实",),
    "E": ("重写一份没有历史的报告",),
    "view": ("把合成资料当作已覆盖真实场景", "没接入的来源写成已覆盖"),
}

Judgment = Literal["A", "B", "C", "D", "E", "view"]
CaseKind = Literal[
    "business_acceptance",  # v3 1.2 各判断的“验收案例”
    "chain_positive",  # 第四部分 正例
    "chain_correction",  # 第四部分 更正
    "chain_anomaly",  # 第四部分 异常
    "chain_judgment_e",  # 第四部分 判断E
    "usage_view",  # 6.4 三种使用视角
]
Chain = Literal["A", "B", "C", "D"]


class DesignRef(BaseModel):
    """一个 v3 出处应当落在哪个判断、哪类案例、哪条链路上。用表固定下来，
    夹具里的 judgment / case_kind / chain 必须与之一致，避免把案例挪到不相干的判断下面凑数。"""

    model_config = ConfigDict(frozen=True)

    judgment: Judgment
    case_kind: CaseKind
    chain: Chain | None = None


def _build_design_refs() -> dict[str, DesignRef]:
    refs: dict[str, DesignRef] = {}
    for j in "ABCDE":
        for n in (1, 2):
            refs[f"v3 1.2 判断{j} 验收案例{n}"] = DesignRef(judgment=j, case_kind="business_acceptance")
    for chain in "ABCD":
        for label, kind in (("正例", "chain_positive"), ("更正", "chain_correction"),
                            ("异常", "chain_anomaly")):
            refs[f"v3 第四部分 链路{chain} {label}"] = DesignRef(judgment=chain, case_kind=kind, chain=chain)
        # 链路上的“判断E”验的是判断更新机制，归到 E
        refs[f"v3 第四部分 链路{chain} 判断E"] = DesignRef(
            judgment="E", case_kind="chain_judgment_e", chain=chain)
    for label in ("使用视角1 国家层", "使用视角2 省级", "使用视角3 高校/科研机构"):
        refs[f"v3 6.4 {label}"] = DesignRef(judgment="view", case_kind="usage_view")
    return refs


REQUIRED_DESIGN_REFS: dict[str, DesignRef] = _build_design_refs()

# 按案例类型必须出现的断言：更正和异常是第四部分逐条点名的，缺一条就不算验过
REQUIRED_CHECKS_BY_KIND: dict[str, tuple[str, ...]] = {
    "chain_correction": ("recompute_only_affected", "keeps_old_judgment"),
    "chain_anomaly": ("no_duplicate_on_restart", "resumes_after_partial_failure",
                      "no_derived_feedback_loop", "identifier_not_double_counted"),
    "chain_judgment_e": ("keeps_old_judgment", "revision_reason_stated"),
    "usage_view": VIEW_EVALUATION_CHECKS + ("no_real_coverage_claim",),
}
REQUIRED_CHECKS_BY_JUDGMENT: dict[str, tuple[str, ...]] = {
    "E": ("keeps_old_judgment", "revision_reason_stated"),
}

EXPECTED_FILES: dict[str, Judgment] = {
    "A.json": "A", "B.json": "B", "C.json": "C", "D.json": "D", "E.json": "E", "views.json": "view",
}

DataBasis = Literal["real_existing", "real_public_replay", "synthetic_flow_only", "to_be_sourced"]
WorkMode = Literal["discovery", "investigation", "judgment_update"]


class AcceptanceContractError(ValueError):
    """夹具集合层面的错误（重复编号、文件缺失、案例放错文件）。继承 ValueError，与单案例校验一致。"""


# ---------- 模型 ----------


class Scenario(BaseModel):
    """一个具体情景及雷达应有的可观察表现。
    expected 写“必须发现/必须说出”的东西；must_not_conclude 写“不能得出”的结论——
    反例的价值全在后者，所以反例里它不能为空。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario: str = Field(min_length=1)
    expected: list[str] = Field(min_length=1)
    must_not_conclude: list[str] = Field(default_factory=list)


class Check(BaseModel):
    """对将来某份报告或某次运行的一条可机检断言。kind 只能取 CHECK_KINDS，
    这样每种断言都能写成固定的判定程序；detail 写清本案例里具体判什么。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str
    detail: str = Field(min_length=1)

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, v: str) -> str:
        if v not in CHECK_KINDS:
            raise ValueError(f"未知断言种类 {v!r}")
        return v


class AcceptanceCase(BaseModel):
    """一个业务验收案例。字段都是必填：缺任何一项，验收就会退化成“看报告感觉对不对”。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # 稳定编号，便于在回放评测和审阅记录里引用；前缀必须与 judgment 对应（视角用 V）
    id: str = Field(pattern=r"^(A|B|C|D|E|V)-\d{2}$")
    # 服务哪个业务判断（A–E）或 6.4 使用视角
    judgment: Judgment
    # 来自 v3 哪一类验收（1.2 验收案例 / 第四部分四项 / 6.4 视角），与 design_ref 表一致
    case_kind: CaseKind
    # 第四部分的纵向链路（A 结构化登记 / B 非结构化文献 / C 历史时序 / D 弱信号）；1.2 与 6.4 案例为空
    chain: Chain | None = None
    # v3 1.1 三种工作方式：主动发现、指定调查和追问、长期判断更新
    work_mode: WorkMode
    # v3 原文出处；每个出处恰好一个案例，防止漏验或重复凑数
    design_ref: str
    # 具体的业务问题（中文）；回答不了它的报告不合格
    question: str = Field(min_length=1)
    # 需要的证据类型（v3 1.4 词表 slug）；来源目录按证据类型组织，不按单一 P 级
    required_evidence: list[str] = Field(min_length=1)
    # 需要的编号和时间字段；时间和实体对不上，同口径比较和同源判断都做不了
    required_entities_and_times: list[str] = Field(min_length=1)
    # 现有能用的来源（含旧插件、旧库存量）；只用来说明“能测什么”，不代表已实测
    sources_available: list[str]
    # 需要但没接入的来源；必须如实列出，报告里也要写成缺口
    sources_gap: list[str]
    # 报告必须说清的内容：1.3 五项 + 本判断点名项（+ 视角的内参结构）
    must_say: list[str] = Field(min_length=1)
    # v3 点名禁止的捷径
    must_not: list[str] = Field(min_length=1)
    # 正例：雷达应当发现/说出的情景
    positive: Scenario
    # 反例：看起来像、但不能得出同样结论的情景；没有反例的验收只会奖励“多报”
    counterexample: Scenario
    # 可机检断言；为空就无法判定通过与否
    checks: list[Check] = Field(min_length=1)
    # 材料从哪来：real_existing（v3 给出的真实且已在库）/ real_public_replay（公开数据回放）/
    # synthetic_flow_only（只验证流程）/ to_be_sourced（真实材料待找，不编造）
    data_basis: DataBasis
    # 需要什么材料、按什么标准找；to_be_sourced 时就是找材料的说明
    material_note: str = Field(min_length=1)
    # 案例里明确标为合成的部分（如 6.4 的内部台账）；有合成部分就不能宣称覆盖真实场景
    synthetic_components: list[str] = Field(default_factory=list)

    @field_validator("required_evidence")
    @classmethod
    def _known_evidence(cls, v: list[str]) -> list[str]:
        bad = [x for x in v if x not in EVIDENCE_TYPES]
        if bad:
            raise ValueError(f"未知证据类型 {bad}（只能用 EVIDENCE_TYPES 的英文 slug）")
        return v

    @field_validator("sources_available", "sources_gap")
    @classmethod
    def _known_sources(cls, v: list[str]) -> list[str]:
        bad = [x for x in v if x not in KNOWN_SOURCES]
        if bad:
            raise ValueError(f"未知来源编号 {bad}")
        if len(set(v)) != len(v):
            raise ValueError(f"来源编号重复 {v}")
        return v

    @model_validator(mode="after")
    def _business_rules(self) -> AcceptanceCase:
        errors: list[str] = []
        prefix = "V" if self.judgment == "view" else self.judgment
        if not self.id.startswith(prefix + "-"):
            errors.append(f"id {self.id} 与 judgment {self.judgment} 不符")

        ref = REQUIRED_DESIGN_REFS.get(self.design_ref)
        if ref is None:
            errors.append(f"design_ref {self.design_ref!r} 不是 v3 的验收出处")
        elif (ref.judgment, ref.case_kind, ref.chain) != (self.judgment, self.case_kind, self.chain):
            errors.append(
                f"design_ref {self.design_ref!r} 应为 judgment={ref.judgment} case_kind={ref.case_kind} "
                f"chain={ref.chain}")

        # 已有的不能写进缺口，缺口不能冒充已有：覆盖现状要与来源目录一致
        for sid in self.sources_available:
            if KNOWN_SOURCES[sid].coverage == "gap":
                errors.append(f"{sid} 在来源目录里是缺口，不能列为 sources_available")
        for sid in self.sources_gap:
            if KNOWN_SOURCES[sid].coverage != "gap":
                errors.append(f"{sid} 在来源目录里不是缺口，不能列为 sources_gap")
        both = set(self.sources_available) & set(self.sources_gap)
        if both:
            errors.append(f"来源同时在 available 和 gap 中：{sorted(both)}")

        need_say = REPORT_MUST_SAY + JUDGMENT_MUST_SAY[self.judgment]
        if self.judgment == "view":
            need_say += BRIEFING_STRUCTURE
        missing_say = [x for x in need_say if x not in self.must_say]
        if missing_say:
            errors.append(f"must_say 缺少 {missing_say}")
        missing_not = [x for x in REQUIRED_MUST_NOT[self.judgment] if x not in self.must_not]
        if missing_not:
            errors.append(f"must_not 缺少 v3 点名的捷径 {missing_not}")

        if not self.counterexample.must_not_conclude:
            errors.append("counterexample.must_not_conclude 不能为空：反例必须写明不能得出的结论")

        kinds = {c.kind for c in self.checks}
        need_checks = {"cites_fixed_version"}
        need_checks |= set(REQUIRED_CHECKS_BY_KIND.get(self.case_kind, ()))
        need_checks |= set(REQUIRED_CHECKS_BY_JUDGMENT.get(self.judgment, ()))
        if self.data_basis == "synthetic_flow_only" or self.synthetic_components:
            need_checks.add("no_real_coverage_claim")
        if self.data_basis == "real_public_replay":
            need_checks.add("visibility_cutoff_respected")
        if self.sources_gap:
            need_checks.add("gap_stated")
        missing_checks = sorted(need_checks - kinds)
        if missing_checks:
            errors.append(f"checks 缺少 {missing_checks}")

        if self.judgment == "E" and self.work_mode != "judgment_update":
            errors.append("判断 E 本身就是长期判断更新，work_mode 必须是 judgment_update")

        if errors:
            raise ValueError(f"案例 {self.id}：" + "；".join(errors))
        return self


# ---------- 入口 ----------


def validate_case(case: AcceptanceCase | dict[str, Any]) -> AcceptanceCase:
    """校验单个案例并返回模型对象。不合格时抛 ValueError（pydantic.ValidationError 也是 ValueError）。"""
    if isinstance(case, AcceptanceCase):
        return case
    return AcceptanceCase.model_validate(case)


def validate_cases(cases: list[AcceptanceCase | dict[str, Any]]) -> list[AcceptanceCase]:
    """校验一组案例，并检查 id 与 design_ref 不重复——重复意味着有案例被复制凑数或被覆盖。"""
    out = [validate_case(c) for c in cases]
    for field in ("id", "design_ref"):
        counts = Counter(getattr(c, field) for c in out)
        dup = sorted(k for k, n in counts.items() if n > 1)
        if dup:
            raise AcceptanceContractError(f"{field} 重复：{dup}")
    return out


def load_all(directory: str | Path) -> list[AcceptanceCase]:
    """读取目录下的 A–E 与 views 六个夹具文件，按文件顺序返回全部案例。

    要求恰好是这六个 JSON 文件：缺文件说明有判断没验，多文件说明有人绕开了约定的分组；
    每个文件只能放对应判断的案例；跨文件的 id、design_ref 也不能重复。
    """
    d = Path(directory)
    present = {p.name for p in d.glob("*.json")}
    missing = sorted(set(EXPECTED_FILES) - present)
    if missing:
        raise AcceptanceContractError(f"缺少夹具文件：{missing}")
    extra = sorted(present - set(EXPECTED_FILES))
    if extra:
        raise AcceptanceContractError(f"未约定的夹具文件：{extra}")

    all_cases: list[AcceptanceCase] = []
    for name, judgment in EXPECTED_FILES.items():
        data = json.loads((d / name).read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise AcceptanceContractError(f"{name} 顶层必须是案例列表")
        for raw in data:
            case = validate_case(raw)
            if case.judgment != judgment:
                raise AcceptanceContractError(f"{name} 里出现了判断 {case.judgment} 的案例 {case.id}")
            all_cases.append(case)
    return validate_cases(all_cases)
