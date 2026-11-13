# -*- coding: utf-8 -*-
"""Agent 联网搜索工具 — 验证实体真实性、查询外部信息。"""

from langchain_core.tools import tool
from loguru import logger


@tool
def web_search(query: str, max_results: int = 5) -> list[dict]:
    """DuckDuckGo 联网搜索，用于验证实体名称真实性、查询药品/公司官方信息。

    Args:
        query: 搜索关键词
        max_results: 最大返回条数 (1-10)

    Returns:
        [{"title": str, "snippet": str, "url": str}] 搜索结果列表
    """
    try:
        from ddgs import DDGS
        with DDGS(timeout=10) as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
            return [
                {
                    "title": r.get("title", ""),
                    "snippet": r.get("body", r.get("snippet", "")),
                    "url": r.get("href", r.get("url", "")),
                }
                for r in results
            ]
    except ImportError:
        logger.error("[AgentTool] ddgs 未安装")
        return [{"title": "错误", "snippet": "联网搜索功能未安装依赖，请执行: pip install ddgs", "url": ""}]
    except Exception as e:
        logger.error(f"[AgentTool] web_search 失败: {e}")
        return [{"title": "搜索不可用", "snippet": "联网搜索暂时不可用（网络受限或超时）", "url": ""}]


@tool
def web_fetch(url: str) -> str:
    """抓取指定 URL 的文本内容（已清理 HTML 标签）。

    Args:
        url: 目标网页 URL

    Returns:
        清理后的页面文本（最多 3000 字符），失败返回空字符串。
    """
    try:
        from fetch.web_client import fetch_page
        result = fetch_page(url, use_simple_fetch=True)
        page = result.get("page")
        if page is None:
            return ""
        text = getattr(page, "cleaned_text", None) or getattr(page, "text", "") or str(page)
        return text[:3000]
    except ImportError:
        logger.error("[AgentTool] scrapling_client 不可用")
        return ""
    except Exception as e:
        logger.error(f"[AgentTool] web_fetch 失败: {e}")
        return ""
