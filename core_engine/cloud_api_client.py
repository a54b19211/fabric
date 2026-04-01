"""
云端 API 客户端 —— 封装 MinerU（PDF 解析）和 Qwen（结构化提取）的请求逻辑。
所有云端调用都集中在这个文件，方便统一管理密钥和错误处理。
"""

import json
import time

import httpx
from openai import OpenAI
from loguru import logger
from prefect import task

from config.settings import APP


# ===================================================================
#  MinerU 云端 API —— PDF → Markdown
# ===================================================================

@task(
    name="mineru_pdf_to_markdown",
    retries=APP.TASK_RETRIES,
    retry_delay_seconds=APP.TASK_RETRY_DELAY,
)
def mineru_pdf_to_markdown(pdf_path: str) -> dict:
    """
    调用 MinerU 商业 API 将 PDF 解析为 Markdown。

    流程：
    1. 上传 PDF 文件 → 获得 task_id
    2. 轮询任务状态直到完成
    3. 下载解析结果（Markdown 文本）

    参数:
        pdf_path: 本地 PDF 文件路径

    返回:
        dict: {"markdown": "解析后的md文本", "success": True/False, "error": ""}
    """
    if not APP.MINERU_API_KEY:
        logger.error("MinerU API Key 未配置，跳过 PDF 解析")
        return {"markdown": "", "success": False, "error": "MINERU_API_KEY 未配置"}

    base = APP.MINERU_API_BASE.rstrip("/")
    headers = {"Authorization": f"Bearer {APP.MINERU_API_KEY}"}

    try:
        with httpx.Client(headers=headers, timeout=300) as client:
            # ---- 步骤 1：上传 PDF ----
            with open(pdf_path, "rb") as f:
                upload_resp = client.post(
                    f"{base}/file/upload",
                    files={"file": (pdf_path.split("/")[-1].split("\\")[-1], f, "application/pdf")},
                )
            upload_resp.raise_for_status()
            task_id = upload_resp.json().get("task_id", "")

            if not task_id:
                return {"markdown": "", "success": False, "error": "未获取到 task_id"}

            logger.info(f"MinerU 上传成功，task_id: {task_id}")

            # ---- 步骤 2：轮询任务状态 ----
            for attempt in range(60):
                time.sleep(5)
                status_resp = client.get(f"{base}/task/{task_id}")
                status_resp.raise_for_status()
                status_data = status_resp.json()

                if status_data.get("status") == "completed":
                    markdown_text = status_data.get("result", {}).get("markdown", "")
                    logger.info(f"MinerU 解析完成，Markdown 长度: {len(markdown_text)}")
                    return {"markdown": markdown_text, "success": True, "error": ""}

                if status_data.get("status") == "failed":
                    err = status_data.get("error", "未知错误")
                    logger.error(f"MinerU 解析失败: {err}")
                    return {"markdown": "", "success": False, "error": err}

            return {"markdown": "", "success": False, "error": "轮询超时（5分钟）"}

    except Exception as e:
        logger.error(f"MinerU API 调用异常: {e}")
        return {"markdown": "", "success": False, "error": str(e)}


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
