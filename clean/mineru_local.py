"""自部署 MinerU（PDF / Office → Markdown）的最小客户端：一个文件 POST 到 <base>/file_parse。地址由调用方给
（clean/attachment_parse.py · mineru_base：DATAFABRIC_MINERU_URL）。云端 MinerU 在 clean/mineru_cloud.py。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from loguru import logger

_MIME_MAP = {
    ".pdf":  "application/pdf",
    ".doc":  "application/pdf",   # MinerU 服务端只认 application/pdf，强制覆盖
    # 自部署 MinerU 3.4 原生解析 docx（11-04 实测），按真实类型发；.doc 仍不支持，由加工层另走云端或转 PDF
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls":  "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def parse(file_path: str, *, base: str, content_list: bool = False, timeout: float = 300) -> dict:
    """按文件扩展名选 MIME type 发给 MinerU 的 /file_parse，返回 {markdown, success, error}（content_list=True 另带
    每个内容块的页码）。支持 .pdf / .doc / .docx / .xls / .xlsx。"""
    base = base.rstrip("/")
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
                    blocks = json.loads(blocks)
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
