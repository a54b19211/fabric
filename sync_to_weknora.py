"""
ZK_Data Fabric → WeKnora 同步脚本

逻辑：
  1. 启动时扫描 data/markdown/ 下所有 .md 文件，生成待处理列表（快照）
  2. 跳过 data/weknora_synced.txt 中已记录的 uuid
  3. 逐条上传，成功后写入 synced 文件；顺便更新 DB 状态（DB 没有该记录则跳过）
  4. 跑完退出，不做守护进程

用法:
    python sync_to_weknora.py
    python sync_to_weknora.py --site nmpa
    python sync_to_weknora.py --dry-run
    python sync_to_weknora.py --reset        # 清空 synced 记录，重新全量同步

环境变量（.env）:
    WEKNORA_BASE_URL
    WEKNORA_API_KEY
    WEKNORA_KB_ID
"""

import os
import sys
import time
import json
import argparse
import pathlib

import httpx
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

WEKNORA_BASE_URL = os.getenv("WEKNORA_BASE_URL", "http://localhost:8080")
WEKNORA_API_KEY  = os.getenv("WEKNORA_API_KEY", "")
WEKNORA_KB_ID    = os.getenv("WEKNORA_KB_ID", "")

MARKDOWN_DIR  = pathlib.Path("data/markdown")
JSON_DIR      = pathlib.Path("data/json_envelopes")
SYNCED_FILE   = pathlib.Path("data/weknora_synced.txt")
REPARSE_QUEUE = pathlib.Path("data/weknora_reparse_queue.txt")

REQUEST_DELAY = 0.3


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def load_synced() -> set:
    if not SYNCED_FILE.exists():
        return set()
    lines = SYNCED_FILE.read_text(encoding="utf-8").splitlines()
    return {l.strip() for l in lines if l.strip()}


def mark_synced(uuid: str) -> None:
    with SYNCED_FILE.open("a", encoding="utf-8") as f:
        f.write(uuid + "\n")


def load_envelope(uuid: str, site: str) -> dict:
    p = JSON_DIR / site / f"{uuid}.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"[WeKnora] 信封加载失败 uuid={uuid} site={site}: {e}")
        return {}


def build_source_metadata(envelope: dict, uuid: str, site: str) -> dict:
    prov = envelope.get("provenance", {})
    conf = envelope.get("confidence", {})
    meta = {
        "source_uuid":      uuid,
        "parent_uuid":      prov.get("parent_uuid", ""),
        "doc_type":         envelope.get("doc_type", ""),
        "source_site":      prov.get("source_site", site),
        "source_url":       prov.get("source_url", ""),
        "published_at":     envelope.get("publish_date", ""),
        "language":         envelope.get("language", ""),
        "drug_name":        envelope.get("drug_name", ""),
        "approval_number":  envelope.get("approval_number", ""),
        "manufacturer":     envelope.get("manufacturer", ""),
        "keywords":         envelope.get("keywords", []),
        "confidence_level": conf.get("level", ""),
        "confidence_score": conf.get("score", 0.0),
        "parse_method":     envelope.get("parse_method", ""),
        "crawl_timestamp":  prov.get("crawl_timestamp", ""),
    }
    return {k: v for k, v in meta.items() if v not in ("", [], None, 0.0)}


def update_db_status(uuid: str, weknora_id: str) -> None:
    """如果 DB 里有该记录，更新 weknora_sync_status。没有则静默跳过。"""
    try:
        from db.session import get_session
        from db.models import Document
        with get_session() as session:
            if session is None:
                return
            doc = session.get(Document, uuid)
            if doc:
                doc.weknora_sync_status = "synced"
                if weknora_id and hasattr(doc, "minio_object_key"):
                    pass  # weknora_id 暂无专用字段，留空
    except Exception as e:
        logger.warning(f"[WeKnora] DB 状态更新失败 uuid={uuid}: {e}")


# ── WeKnora 客户端 ────────────────────────────────────────────────────────────

class WeKnoraClient:
    def __init__(self):
        self.base = WEKNORA_BASE_URL.rstrip("/")
        self.headers = {"X-API-Key": WEKNORA_API_KEY}

    def ping(self):
        resp = httpx.get(f"{self.base}/api/v1/knowledge-bases", headers=self.headers, timeout=10)
        if resp.status_code == 401:
            raise RuntimeError("API Key 无效")
        resp.raise_for_status()

    def create_manual(self, title: str, content: str, source_metadata: dict) -> str:
        """上传，返回 weknora knowledge id。"""
        resp = httpx.post(
            f"{self.base}/api/v1/knowledge-bases/{WEKNORA_KB_ID}/knowledge/manual",
            headers=self.headers,
            json={"title": title, "content": content, "channel": "crawler", "source_metadata": source_metadata},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"WeKnora 返回失败: {data}")
        return data["data"].get("id", "")


# ── 主流程 ────────────────────────────────────────────────────────────────────

def main(site_filter: str | None, dry_run: bool):
    if not WEKNORA_KB_ID or not WEKNORA_API_KEY:
        logger.error("请在 .env 中配置 WEKNORA_KB_ID 和 WEKNORA_API_KEY")
        sys.exit(1)

    client = WeKnoraClient()
    try:
        client.ping()
        logger.info(f"连通性检查通过: {WEKNORA_BASE_URL}")
    except Exception as e:
        logger.error(f"连通性检查失败: {e}")
        sys.exit(1)

    # 启动时快照：扫描所有 .md 文件
    all_files: list[tuple[str, str, pathlib.Path]] = []  # (uuid, site, path)
    for site_dir in sorted(MARKDOWN_DIR.iterdir()):
        if not site_dir.is_dir():
            continue
        if site_filter and site_dir.name != site_filter:
            continue
        for md_file in sorted(site_dir.glob("*.md")):
            all_files.append((md_file.stem, site_dir.name, md_file))

    # 加载已同步记录
    synced = load_synced()
    todo = [(uuid, site, path) for uuid, site, path in all_files if uuid not in synced]

    logger.info(f"共 {len(all_files)} 个文件，已同步 {len(synced)}，本次待处理 {len(todo)}")

    if not todo:
        logger.info("无待同步记录，退出")
        return

    success = failed = skipped = 0
    for uuid, site, md_path in todo:
        envelope = load_envelope(uuid, site)
        title = envelope.get("title") or uuid
        source_metadata = build_source_metadata(envelope, uuid, site)

        if dry_run:
            logger.info(f"[DryRun] {site}/{uuid} | {title[:50]}")
            skipped += 1
            continue

        try:
            content = md_path.read_text(encoding="utf-8")
            weknora_id = client.create_manual(title, content, source_metadata)
            logger.info(f"✓ {site}/{uuid} → {weknora_id}")
            mark_synced(uuid)
            synced.add(uuid)
            update_db_status(uuid, weknora_id)
            # 写入 reparse 队列
            if weknora_id:
                with REPARSE_QUEUE.open("a", encoding="utf-8") as f:
                    f.write(weknora_id + "\n")
            success += 1
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 409:
                logger.info(f"已存在跳过: {uuid}")
                mark_synced(uuid)
                synced.add(uuid)
                update_db_status(uuid, "")
                skipped += 1
            else:
                logger.error(f"✗ {uuid}: HTTP {e.response.status_code} {e.response.text[:100]}")
                failed += 1
        except Exception as e:
            logger.error(f"✗ {uuid}: {e}")
            failed += 1

        time.sleep(REQUEST_DELAY)

    logger.info(f"完成 | 成功={success} 跳过={skipped} 失败={failed}")


# ── 命令行 ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")

    parser = argparse.ArgumentParser()
    parser.add_argument("--site",    default=None,         help="只同步指定站点")
    parser.add_argument("--dry-run", action="store_true",  help="预览，不实际上传")
    parser.add_argument("--reset",   action="store_true",  help="清空 synced 记录，重新全量同步")
    args = parser.parse_args()

    if args.reset:
        if SYNCED_FILE.exists():
            SYNCED_FILE.unlink()
            logger.info(f"已清空: {SYNCED_FILE}")
        sys.exit(0)

    main(site_filter=args.site, dry_run=args.dry_run)
