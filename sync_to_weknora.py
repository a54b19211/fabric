"""
==========================================================
  ZK_Data Fabric → WeKnora 数据同步脚本
==========================================================

将本地 data/markdown/ 下的 .md 文件上传到 WeKnora 知识库。

用法:
    python sync_to_weknora.py                        # 同步所有待同步记录
    python sync_to_weknora.py --site nmpa            # 只同步指定站点
    python sync_to_weknora.py --limit 100            # 限制本次同步条数
    python sync_to_weknora.py --dry-run              # 预览，不实际上传
    python sync_to_weknora.py --reset-failed         # 重置失败记录，重新同步

环境变量（或 .env）:
    WEKNORA_BASE_URL    WeKnora 服务地址，如 http://localhost:8080
    WEKNORA_API_KEY     WeKnora API Key
    WEKNORA_KB_ID       目标知识库 ID，如 kb-00000001
"""

import os
import sys
import time
import json
import argparse
import pathlib
from datetime import datetime, timezone

import httpx
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

# ── 配置 ──────────────────────────────────────────────────────────────────────
WEKNORA_BASE_URL = os.getenv("WEKNORA_BASE_URL", "http://localhost:8080")
WEKNORA_API_KEY  = os.getenv("WEKNORA_API_KEY", "")
WEKNORA_KB_ID    = os.getenv("WEKNORA_KB_ID", "")

MARKDOWN_DIR     = pathlib.Path("data/markdown")
JSON_DIR         = pathlib.Path("data/json_envelopes")

REQUEST_DELAY    = 0.3   # 每次上传间隔（秒），避免打爆 WeKnora
POLL_INTERVAL    = 2.0   # 轮询解析状态间隔（秒）
POLL_TIMEOUT     = 120   # 最长等待解析完成时间（秒）


# ── WeKnora API 客户端 ─────────────────────────────────────────────────────────

class WeKnoraClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-API-Key": api_key}

    def upload_file(self, kb_id: str, md_path: pathlib.Path, metadata: dict) -> dict:
        """上传 markdown 文件到 WeKnora 知识库，返回 knowledge 对象。"""
        url = f"{self.base_url}/api/v1/knowledge-bases/{kb_id}/knowledge/file"
        with open(md_path, "rb") as f:
            files = {"file": (md_path.name, f, "text/markdown")}
            data  = {"metadata": json.dumps(metadata, ensure_ascii=False)}
            resp  = httpx.post(url, headers=self.headers, files=files, data=data, timeout=60)
        resp.raise_for_status()
        result = resp.json()
        if not result.get("success"):
            raise RuntimeError(f"WeKnora 返回失败: {result}")
        return result["data"]

    def get_knowledge(self, knowledge_id: str) -> dict:
        """查询单条 knowledge 的解析状态。"""
        url  = f"{self.base_url}/api/v1/knowledge/{knowledge_id}"
        resp = httpx.get(url, headers=self.headers, timeout=30)
        resp.raise_for_status()
        return resp.json()["data"]

    def wait_for_parse(self, knowledge_id: str) -> str:
        """
        轮询等待 WeKnora 完成解析。
        返回最终状态: 'completed' | 'failed' | 'timeout'
        """
        deadline = time.time() + POLL_TIMEOUT
        while time.time() < deadline:
            time.sleep(POLL_INTERVAL)
            try:
                k = self.get_knowledge(knowledge_id)
                status = k.get("parse_status", "")
                if status in ("completed", "failed"):
                    return status
            except Exception as e:
                logger.warning(f"轮询失败: {knowledge_id} | {e}")
        return "timeout"


# ── 本地数据库状态更新 ─────────────────────────────────────────────────────────

def _update_sync_status(uuid: str, status: str, weknora_id: str = ""):
    """更新 documents 表的 weknora_sync_status 和 weknora_knowledge_id。"""
    try:
        from db.session import get_session
        from db.models import Document
        with get_session() as session:
            if session is None:
                return
            doc = session.get(Document, uuid)
            if doc:
                doc.weknora_sync_status = status
                if weknora_id:
                    # weknora_knowledge_id 字段需要先迁移加上，暂时存在 minio_object_key 备用字段
                    pass
    except Exception as e:
        logger.warning(f"更新 DB 状态失败: {uuid} | {e}")


# ── 核心同步逻辑 ───────────────────────────────────────────────────────────────

def _load_json_envelope(uuid: str, site: str) -> dict | None:
    """读取对应的 json_envelope，提取 metadata。"""
    p = JSON_DIR / site / f"{uuid}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _build_metadata(envelope: dict) -> dict:
    """从 json_envelope 构建 WeKnora metadata 字典（map[string]string）。"""
    meta = {
        "source_site":    envelope.get("provenance", {}).get("source_site", ""),
        "source_url":     envelope.get("provenance", {}).get("source_url", ""),
        "publish_date":   envelope.get("publish_date", ""),
        "doc_type":       envelope.get("doc_type", ""),
        "language":       envelope.get("language", ""),
        "confidence_level": envelope.get("confidence", {}).get("level", ""),
        "local_uuid":     envelope.get("uuid", ""),
    }
    # 过滤空值
    return {k: v for k, v in meta.items() if v}


def _iter_pending(site_filter: str | None, limit: int | None):
    """
    遍历 data/markdown/ 下所有待同步的 .md 文件。
    优先从 DB 读取 weknora_sync_status='pending' 的记录；
    DB 不可用时降级为扫描本地文件。
    """
    try:
        from db.session import get_session
        from db.models import Document
        from sqlalchemy import select

        with get_session() as session:
            if session is None:
                raise RuntimeError("DB 不可用")
            stmt = select(Document).where(Document.weknora_sync_status == "pending")
            if site_filter:
                stmt = stmt.where(Document.source_site == site_filter)
            if limit:
                stmt = stmt.limit(limit)
            docs = session.execute(stmt).scalars().all()

        for doc in docs:
            md_path = MARKDOWN_DIR / doc.source_site / f"{doc.uuid}.md"
            if not md_path.exists():
                logger.warning(f"[Skip] markdown 文件不存在: {md_path}")
                continue
            envelope = _load_json_envelope(doc.uuid, doc.source_site)
            yield doc.uuid, doc.source_site, md_path, envelope

    except Exception as e:
        logger.warning(f"DB 查询失败，降级扫描本地文件: {e}")
        # 降级：扫描 markdown 目录
        count = 0
        for site_dir in sorted(MARKDOWN_DIR.iterdir()):
            if not site_dir.is_dir():
                continue
            if site_filter and site_dir.name != site_filter:
                continue
            for md_file in sorted(site_dir.glob("*.md")):
                uuid = md_file.stem
                envelope = _load_json_envelope(uuid, site_dir.name)
                yield uuid, site_dir.name, md_file, envelope
                count += 1
                if limit and count >= limit:
                    return


def sync(
    site_filter: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    wait_parse: bool = False,
):
    """主同步函数。"""
    if not WEKNORA_KB_ID:
        logger.error("未配置 WEKNORA_KB_ID，请在 .env 中设置")
        sys.exit(1)
    if not WEKNORA_API_KEY:
        logger.error("未配置 WEKNORA_API_KEY，请在 .env 中设置")
        sys.exit(1)

    client = WeKnoraClient(WEKNORA_BASE_URL, WEKNORA_API_KEY)
    stats  = {"total": 0, "success": 0, "failed": 0, "skipped": 0}

    for uuid, site, md_path, envelope in _iter_pending(site_filter, limit):
        stats["total"] += 1
        metadata = _build_metadata(envelope) if envelope else {"local_uuid": uuid, "source_site": site}

        logger.info(f"[Sync] {site}/{uuid} → {md_path.name}")

        if dry_run:
            logger.info(f"  [DryRun] metadata={metadata}")
            stats["skipped"] += 1
            continue

        try:
            knowledge = client.upload_file(WEKNORA_KB_ID, md_path, metadata)
            weknora_id = knowledge.get("id", "")
            logger.info(f"  ✓ 上传成功: weknora_id={weknora_id}, parse_status={knowledge.get('parse_status')}")

            if wait_parse and weknora_id:
                final_status = client.wait_for_parse(weknora_id)
                logger.info(f"  解析完成: {final_status}")

            _update_sync_status(uuid, "synced", weknora_id)
            stats["success"] += 1

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 409:
                logger.info(f"  已存在，跳过: {uuid}")
                _update_sync_status(uuid, "synced")
                stats["skipped"] += 1
            else:
                logger.error(f"  ✗ 上传失败: {e}")
                _update_sync_status(uuid, "failed")
                stats["failed"] += 1
        except Exception as e:
            logger.error(f"  ✗ 异常: {e}")
            _update_sync_status(uuid, "failed")
            stats["failed"] += 1

        time.sleep(REQUEST_DELAY)

    logger.info(
        f"\n同步完成 | 总计={stats['total']} "
        f"成功={stats['success']} 失败={stats['failed']} 跳过={stats['skipped']}"
    )


# ── 命令行入口 ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")

    parser = argparse.ArgumentParser(description="ZK_Data Fabric → WeKnora 同步工具")
    parser.add_argument("--site",         default=None,  help="只同步指定站点，如 nmpa")
    parser.add_argument("--limit",        type=int, default=None, help="本次最多同步条数")
    parser.add_argument("--dry-run",      action="store_true",    help="预览，不实际上传")
    parser.add_argument("--wait-parse",   action="store_true",    help="等待 WeKnora 解析完成")
    parser.add_argument("--reset-failed", action="store_true",    help="将 failed 状态重置为 pending")
    args = parser.parse_args()

    if args.reset_failed:
        try:
            from db.session import get_session
            from db.models import Document
            with get_session() as session:
                if session:
                    docs = session.query(Document).filter(Document.weknora_sync_status == "failed").all()
                    for d in docs:
                        d.weknora_sync_status = "pending"
                    logger.info(f"已重置 {len(docs)} 条 failed 记录为 pending")
        except Exception as e:
            logger.error(f"重置失败: {e}")
        sys.exit(0)

    sync(
        site_filter=args.site,
        limit=args.limit,
        dry_run=args.dry_run,
        wait_parse=args.wait_parse,
    )
