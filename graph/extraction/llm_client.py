"""
API 客户端 —— 封装 MinerU（PDF 解析）和 Qwen（结构化提取）的请求逻辑。

非流式的对话调用（qwen_extract_structured、qwen_chat）的 HTTP 一侧是共享的 llm_transport；流式调试输出和 embedding
仍走 OpenAI SDK。
"""

import json
import tempfile
import time
from pathlib import Path

import httpx
import llm_transport as lt
from loguru import logger
from openai import OpenAI
from prefect import task

from settings import APP


def _format_json_error(raw: str, exc: json.JSONDecodeError) -> str:
    """生成 JSON 解析错误的详细诊断信息，定位到出错位置附近。"""
    pos = exc.pos
    start = max(0, pos - 200)
    end = min(len(raw), pos + 200)
    snippet = raw[start:end]
    pointer = " " * (pos - start) + "^-- HERE"

    # 计算出错位置的行号（基于原字符串）
    before = raw[:pos]
    line_num = before.count("\n") + 1
    last_nl = before.rfind("\n")
    col_num = pos - (last_nl + 1) if last_nl >= 0 else pos

    lines = [
        f"JSON 解析失败: {exc.msg}",
        f"位置: 第 {line_num} 行, 第 {col_num} 列 (char {pos})",
        "上下文:",
        "```json",
        f"{snippet}",
        f"{pointer}",
        "```",
    ]
    return "\n".join(lines)


def _strip_json_fence(raw: str) -> str:
    """剥离 LLM 输出中可能包裹的 ```json ... ``` 围栏。"""
    s = raw.strip()
    if s.startswith("```json"):
        s = s[7:]
    elif s.startswith("```"):
        s = s[3:]
    if s.endswith("```"):
        s = s[:-3]
    return s.strip()


# ===================================================================
#  MinerU 自部署 API —— PDF → Markdown
# ===================================================================

_MIME_MAP = {
    ".pdf":  "application/pdf",
    ".doc":  "application/pdf",   # MinerU 服务端只认 application/pdf，强制覆盖
    # 自部署 MinerU 3.4 原生解析 docx（11-04 实测），按真实类型发；.doc 仍不支持，由加工层另走云端或转 PDF
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls":  "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _mineru_parse(file_path: str, base: str | None = None, content_list: bool = False, timeout: float = 300) -> dict:
    """
    通用 MinerU 解析内核，根据文件扩展名自动选择 MIME type。
    支持 .pdf / .doc / .docx / .xls / .xlsx。
    """
    # base 可由调用方给（加工层 attachment_parse 用 DATAFABRIC_MINERU_URL）；默认沿用旧配置
    base = (base or APP.MINERU_API_BASE).rstrip("/")
    path_obj = Path(file_path)
    fname = path_obj.name
    mime = _MIME_MAP.get(path_obj.suffix.lower(), "application/octet-stream")
    logger.info(f"[MinerU] 同步解析: {file_path} (mime={mime})")
    try:
        with open(file_path, "rb") as f:
            resp = httpx.post(
                f"{base}/file_parse",
                files=[("files", (fname, f, mime))],
                # content_list 给出每个内容块的页码（page_idx），加工层用它做定位
                data={"return_md": "true", "backend": "pipeline",
                      **({"return_content_list": "true"} if content_list else {})},
                timeout=timeout,
            )
        resp.raise_for_status()

        data = resp.json()
        markdown = ""
        blocks = None
        if isinstance(data, dict):
            results = data.get("results", {})
            if isinstance(results, dict) and results:
                first = next(iter(results.values()))
                markdown = first.get("md_content") or first.get("md") or first.get("markdown") or ""
                blocks = first.get("content_list")
                if isinstance(blocks, str):
                    import json as _json

                    blocks = _json.loads(blocks)
            if not markdown:
                markdown = data.get("md_content") or data.get("md") or data.get("markdown") or ""
        elif isinstance(data, list) and data:
            markdown = data[0].get("md_content") or data[0].get("md") or data[0].get("markdown") or ""
        elif isinstance(data, str):
            markdown = data

        if not markdown:
            return {"markdown": "", "success": False, "error": f"响应中未找到 markdown 内容，原始响应: {resp.text[:300]}"}
        logger.info(f"[MinerU] 解析完成，Markdown 长度: {len(markdown)}")
        out = {"markdown": markdown, "success": True, "error": ""}
        if content_list:
            out["content_list"] = blocks if isinstance(blocks, list) else None
        return out
    except Exception as e:
        logger.error(f"[MinerU] 调用异常: {e}")
        return {"markdown": "", "success": False, "error": str(e)}


# ===================================================================
#  Qwen 大模型 API —— Markdown → 结构化 JSON
# ===================================================================

# What the OpenAI SDK did for these calls and they keep: a new client per call that follows redirects, 600 s per
# request with 5 s to connect, two resends (0.5 s, then 1 s later) after a timeout, a connection failure or status
# 408/409/429/5xx, and the SDK's error text. ADR 0007 lists what is not kept.
_TIMEOUT_SECONDS = 600.0
_CONNECT_SECONDS = 5.0
_RESEND_WAITS = (0.5, 1.0)


class ChatFailed(Exception):
    """A chat request that gave no usable completion; the text is the OpenAI SDK's for the same outcome."""


def _connect_timeout(request: httpx.Request) -> None:
    # llm_transport gives every phase the provider's timeout; connecting keeps the SDK's shorter limit
    request.extensions["timeout"] = {**request.extensions["timeout"], "connect": _CONNECT_SECONDS}


def _resend(sent: lt.Attempt) -> bool:
    return sent.exception is not None or sent.status in (408, 409, 429) or (sent.status or 0) >= 500


def _failure(sent: lt.Attempt) -> str:
    if sent.exception is not None:
        return "Request timed out." if sent.timed_out else "Connection error."
    text = (sent.response_text or "").strip()
    try:
        return f"Error code: {sent.status} - {json.loads(text)}"
    except ValueError:
        return text or f"Error code: {sent.status}"


def _chat_completion(stage: str, api_key: str, base_url: str, body: dict) -> str | None:
    """The first choice's message content for one chat request, resent as above; raises ChatFailed otherwise."""
    provider = lt.Provider(kind="openai_compatible", base_url=base_url, model=body["model"], api_key=api_key,
                           timeout_seconds=_TIMEOUT_SECONDS)
    record = lt.CallRecord(stage=stage, endpoint_id="", model=provider.model)
    with httpx.Client(follow_redirects=True, event_hooks={"request": [_connect_timeout]}) as client:
        for wait in (*_RESEND_WAITS, None):
            sent = lt.send(provider, body, record, client=client)
            if wait is None or not _resend(sent):
                break
            time.sleep(wait)
    if sent.status is None or not 200 <= sent.status < 300:
        raise ChatFailed(_failure(sent))
    answer = sent.envelope()
    if answer.problem:
        raise ChatFailed(f"unreadable chat completion ({answer.problem})")
    return answer.content


@task(
    name="qwen_extract_structured",
    retries=APP.TASK_RETRIES,
    retry_delay_seconds=APP.TASK_RETRY_DELAY,
)
def qwen_extract_structured(
    prompt: str,
    system_prompt: str = "",
    use_extraction_model: bool = False,
    stream: bool = False,
) -> dict:
    """
    调用 LLM 进行结构化信息提取（OpenAI 兼容协议）。

    参数:
        prompt:               完整 user prompt
        system_prompt:        自定义 system prompt，为空时使用默认值
        use_extraction_model: True 时使用 EXTRACTION_* 配置的模型
        stream:               True 时开启流式输出，token 实时打印到终端

    返回:
        dict: 解析后的 JSON，失败时返回空 dict
    """
    api_key = APP.EXTRACTION_API_KEY if use_extraction_model else APP.LLM_API_KEY
    base_url = APP.EXTRACTION_API_BASE if use_extraction_model else APP.LLM_API_BASE
    model_name = APP.EXTRACTION_MODEL_NAME if use_extraction_model else APP.LLM_MODEL_NAME

    if not api_key:
        logger.error("LLM API Key 未配置（LLM_API_KEY），跳过结构化提取")
        return {}

    default_system = "你是专业的医药文档信息提取助手。直接输出纯 JSON 文本（以 { 开头），禁止用 ```json 代码块包裹，禁止添加任何解释或额外文字。"
    messages = [
        {"role": "system", "content": system_prompt or default_system},
        {"role": "user", "content": prompt},
    ]

    try:
        if stream:
            return _stream_extract(OpenAI(api_key=api_key, base_url=base_url), model_name, messages)
        return _batch_extract(api_key, base_url, model_name, messages)

    except Exception as e:
        logger.error(f"LLM API 调用异常: {e}")
        return {}


def _batch_extract(api_key: str, base_url: str, model_name: str, messages: list) -> dict:
    """非流式调用，等待完整响应后返回。"""
    try:
        content = _chat_completion("qwen_extract_structured", api_key, base_url, {
            "model": model_name,
            "messages": messages,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        })
        raw_content = _strip_json_fence(content)
        result = json.loads(raw_content)
        logger.info(f"LLM 提取成功，模型={model_name}，字段数={len(result)}")
        return result
    except json.JSONDecodeError as e:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
        tmp.write(raw_content)
        tmp.close()
        logger.error(f"LLM 返回内容非合法 JSON\n{_format_json_error(raw_content, e)}\n完整内容(原始): {tmp.name}")
        return {}


def _stream_extract(client: OpenAI, model_name: str, messages: list) -> dict:
    """
    流式调用，token 实时打印到终端，完成后解析完整 JSON 返回。
    适合命令行调试时观察 LLM 实时输出。
    """
    print(f"\n[LLM 流式输出] 模型={model_name}", flush=True)
    print("-" * 60, flush=True)

    full_content = ""
    try:
        stream = client.chat.completions.create(
            model=model_name,
            messages=messages,
            temperature=0.1,
            stream=True,
            # 注意：部分模型流式模式不支持 response_format，去掉该参数
        )
        for chunk in stream:
            delta = chunk.choices[0].delta
            token = getattr(delta, "content", "") or ""
            if token:
                print(token, end="", flush=True)
                full_content += token

        print("\n" + "-" * 60, flush=True)
        logger.info(f"[LLM] 流式输出完成，总长度={len(full_content)}")

        # 解析完整 JSON（先剥离可能的围栏）
        clean_content = _strip_json_fence(full_content)
        result = json.loads(clean_content)
        logger.info(f"LLM 提取成功，模型={model_name}，字段数={len(result)}")
        return result

    except json.JSONDecodeError as e:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
        tmp.write(full_content)
        tmp.close()
        logger.error(f"LLM 流式输出非合法 JSON\n{_format_json_error(clean_content, e)}\n完整内容(原始): {tmp.name}")
        return {}
    except Exception as e:
        logger.error(f"LLM 流式调用异常: {e}")
        return {}


# ===================================================================
#  Embedding API —— 文本 → 向量（消歧向量检索用）
# ===================================================================

def generate_embedding(text: str) -> list[float] | None:
    """
    调用 DashScope text-embedding-v3 API（OpenAI 兼容 /embeddings 端点）。
    返回维度 = APP.EMBEDDING_DIMENSIONS 的 float 列表，失败返回 None。
    """
    api_key = APP.EMBEDDING_KEY
    if not api_key:
        logger.error("Embedding API Key 未配置，跳过 embedding 生成")
        return None

    try:
        client = OpenAI(api_key=api_key, base_url=APP.EMBEDDING_API_BASE)
        response = client.embeddings.create(
            model=APP.EMBEDDING_MODEL_NAME,
            input=text,
            dimensions=APP.EMBEDDING_DIMENSIONS,
        )
        embedding = response.data[0].embedding
        logger.debug(f"[Embedding] 生成成功, dims={len(embedding)}")
        return embedding
    except Exception as e:
        logger.error(f"[Embedding] 生成失败: {e}")
        return None


def qwen_chat(
    system_prompt: str = "",
    user_message: str = "",
    temperature: float = 0.0,
    max_tokens: int = 500,
) -> str:
    """
    轻量 LLM 对话，用于消歧 LLM 兜底等非结构化场景。
    返回 LLM 文本回复，失败返回空字符串。
    """
    if not APP.LLM_API_KEY:
        logger.error("LLM API Key 未配置")
        return ""
    try:
        content = _chat_completion("qwen_chat", APP.LLM_API_KEY, APP.LLM_API_BASE, {
            "model": APP.LLM_MODEL_NAME,
            "messages": [
                {"role": "system", "content": system_prompt or ""},
                {"role": "user", "content": user_message},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        })
        return content or ""
    except Exception as e:
        logger.error(f"qwen_chat 调用失败: {e}")
        return ""


def build_embedding_text(
    entity_name: str,
    entity_type: str,
    entity_data: dict | None = None,
) -> str:
    """
    构造用于 embedding 的文本，组合实体类型、名称、别名和关键 ID 字段。
    不只用名称，避免 PD-1 和 PD-L1 向量相近但非同实体的误判。
    """
    parts = [f"实体类型: {entity_type}", f"名称: {entity_name}"]

    if entity_data:
        name_en = entity_data.get("name_en") or entity_data.get("name")
        if name_en and name_en != entity_name:
            parts.append(f"英文名: {name_en}")

        aliases = entity_data.get("aliases") or []
        if aliases:
            parts.append(f"别名: {', '.join(str(a) for a in aliases[:5])}")

        key_fields_map = {
            "Company":       ["stock_code"],
            "Drug":          ["cas_number"],
            "Target":        ["gene_symbol"],
            "ClinicalTrial": ["trial_id"],
            "Compound":      ["cas_number"],
            "Policy":        ["doc_number"],
            "Pathway":       [],
        }
        for f in key_fields_map.get(entity_type, []):
            val = entity_data.get(f)
            if val:
                parts.append(f"{f}={val}")

    return "\n".join(parts)
