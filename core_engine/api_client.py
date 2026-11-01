"""
通用 API 拉取客户端。

对外只暴露一个接口：iter_api_records(profile)
具体的分页逻辑、请求构造由站点配置中的 api_fetcher 函数提供。

站点配置示例：
    from biz_rules.extractors.fda.api_enforcement import fetch_enforcement_pages

    CONFIG = {
        "source_type": "api",
        "api_fetcher": fetch_enforcement_pages,   # 必须是 generator function
        ...
    }

api_fetcher 签名约定：
    def fetch_xxx(config: dict) -> Iterator[dict]:
        \"\"\"
        接收站点配置，逐条 yield 原始记录 dict。
        分页、限流、重试等逻辑全部在这里实现。
        \"\"\"
"""

from typing import Iterator
from loguru import logger


def iter_api_records(profile: dict) -> Iterator[dict]:
    """
    通用 API 记录迭代器。
    调用站点配置中的 api_fetcher，逐条 yield 原始记录。
    """
    fetcher = profile.get("api_fetcher")
    if fetcher is None:
        raise ValueError(
            f"站点配置缺少 api_fetcher，source_type='api' 的站点必须提供此函数"
        )
    if not callable(fetcher):
        raise TypeError(f"api_fetcher 必须是可调用对象，当前类型: {type(fetcher)}")

    api_cfg = profile.get("api", {})
    logger.info(f"[API] 开始拉取，fetcher={fetcher.__name__}")

    yield from fetcher(api_cfg)
