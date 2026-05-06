"""
API 客户端 —— 封装 MinerU（PDF 解析）和 Qwen（结构化提取）的请求逻辑。
"""

import json
from pathlib import Path

import httpx
from openai import OpenAI
from loguru import logger
from prefect import task

from config.settings import APP


# ===================================================================
#  MinerU 自部署 API —— PDF → Markdown
# ===================================================================

_MIME_MAP = {
    ".pdf":  "application/pdf",
    ".doc":  "application/pdf",   # MinerU 服务端只认 application/pdf，强制覆盖
    ".docx": "application/pdf",   # 同上
}


def _mineru_parse(file_path: str) -> dict:
    """
    通用 MinerU 解析内核，根据文件扩展名自动选择 MIME type。
    支持 .pdf / .doc / .docx。
    """
    base = APP.MINERU_API_BASE.rstrip("/")
    path_obj = Path(file_path)
    fname = path_obj.name
    mime = _MIME_MAP.get(path_obj.suffix.lower(), "application/octet-stream")
    logger.info(f"[MinerU] 同步解析: {file_path} (mime={mime})")
    try:
        with open(file_path, "rb") as f:
            resp = httpx.post(
                f"{base}/file_parse",
                files=[("files", (fname, f, mime))],
                data={"return_md": "true", "backend": "pipeline"},
                timeout=300,
            )
        resp.raise_for_status()

        data = resp.json()
        markdown = ""
        if isinstance(data, dict):
            results = data.get("results", {})
            if isinstance(results, dict) and results:
                first = next(iter(results.values()))
                markdown = first.get("md_content") or first.get("md") or first.get("markdown") or ""
            if not markdown:
                markdown = data.get("md_content") or data.get("md") or data.get("markdown") or ""
        elif isinstance(data, list) and data:
            markdown = data[0].get("md_content") or data[0].get("md") or data[0].get("markdown") or ""
        elif isinstance(data, str):
            markdown = data

        if not markdown:
            return {"markdown": "", "success": False, "error": f"响应中未找到 markdown 内容，原始响应: {resp.text[:300]}"}
        logger.info(f"[MinerU] 解析完成，Markdown 长度: {len(markdown)}")
        return {"markdown": markdown, "success": True, "error": ""}
    except Exception as e:
        logger.error(f"[MinerU] 调用异常: {e}")
        return {"markdown": "", "success": False, "error": str(e)}


@task(
    name="mineru_pdf_to_markdown",
    retries=APP.TASK_RETRIES,
    retry_delay_seconds=APP.TASK_RETRY_DELAY,
)
def mineru_pdf_to_markdown(pdf_path: str) -> dict:
    """
    调用自部署 MinerU 同步接口将 PDF / DOC / DOCX 解析为 Markdown。
        POST /file_parse → 直接返回结果

    参数:
        pdf_path: 本地文件路径（.pdf / .doc / .docx）

    返回:
        dict: {"markdown": "...", "success": True/False, "error": ""}
    """
    return _mineru_parse(pdf_path)


# ===================================================================
#  Qwen 大模型 API —— Markdown → 结构化 JSON
# ===================================================================

def _build_qwen_client() -> OpenAI:
    """构建兼容 OpenAI 协议的 Qwen 客户端。"""
    return OpenAI(
        api_key=APP.QWEN_API_KEY,
        base_url=APP.QWEN_API_BASE,
    )


@task(
    name="qwen_extract_structured",
    retries=APP.TASK_RETRIES,
    retry_delay_seconds=APP.TASK_RETRY_DELAY,
)
def qwen_extract_structured(prompt: str) -> dict:
    """
    调用 Qwen（通义千问）进行结构化信息提取。

    参数:
        prompt: 拼好的完整 prompt（由 biz_rules/prompts.py 生成）

    返回:
        dict: Qwen 返回的解析后 JSON，失败时返回空 dict
    """
    if not APP.QWEN_API_KEY:
        logger.error("Qwen API Key 未配置，跳过结构化提取")
        return {}

    try:
        client = _build_qwen_client()
        response = client.chat.completions.create(
            model=APP.QWEN_MODEL_NAME,
            messages=[
                {"role": "system", "content": "你是专业的医药文档信息提取助手，请严格按 JSON 格式返回。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            response_format={"type": "json_object"},
        )

        raw_content = response.choices[0].message.content
        result = json.loads(raw_content)
        logger.info(f"Qwen 结构化提取成功，字段数: {len(result)}")
        return result

    except json.JSONDecodeError as e:
        logger.error(f"Qwen 返回内容非合法 JSON: {e}")
        return {}
    except Exception as e:
        logger.error(f"Qwen API 调用异常: {e}")
        return {}
