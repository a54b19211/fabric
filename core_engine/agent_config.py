# -*- coding: utf-8 -*-
"""Agent 全局配置 — 从现有 APP settings 读取 LLM 配置，覆盖 Agent 专用阈值。"""

from dataclasses import dataclass, field

from config.settings import APP


@dataclass
class AgentConfig:
    # ── LLM 配置（从 APP settings 读取） ──
    model_name: str = APP.LLM_MODEL_NAME
    llm_api_key: str = APP.LLM_API_KEY
    llm_api_base: str = APP.LLM_API_BASE
    temperature: float = 0.0
    max_tokens: int = 8192
    model_kwargs: dict = field(default_factory=dict)

    # ── ReAct 控制（Agent B/C 使用） ──
    max_iterations: int = 25
    max_concurrent_reviews: int = 5

    # ── 来源权威度（值越高越权威） ──
    source_authority: dict = field(default_factory=lambda: {
        "nmpa": 10,
        "cde": 9,
        "cde_zdyz": 9,
        "cde_xxgk": 9,
        "fda_drug_safety": 9,
        "fda_api_drug_label": 9,
        "fda_api_enforcement": 9,
        "drugbank": 8,
        "pubmed": 7,
        "clinicaltrials": 6,
        "chictr": 6,
        "phirda_policy": 5,
        "phirda_industry": 4,
        "phirda_rd": 4,
        "phirda_healthcare_reform": 4,
        "fiercepharma": 4,
        "hkex": 3,
        "roche": 3,
    })

    # ── 语义匹配 ──
    semantic_match_min_similarity: float = 0.85
    merge_confidence_min: float = 0.8

    def get_source_authority(self, source_site: str) -> int:
        return self.source_authority.get(source_site, 0)
