"""
WeKnora reparse 索引触发脚本

逻辑：
  1. 启动时读取 data/weknora_reparse_queue.txt 的当前内容（快照）
  2. 逐条触发 reparse，成功写入 done 文件，失败保留在队列
  3. 跑完退出

用法:
    python reparse_weknora.py
    python reparse_weknora.py --dry-run

环境变量（.env）:
    WEKNORA_BASE_URL
    WEKNORA_API_KEY
"""

import os
import sys
import time
import argparse
import pathlib

import httpx
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

WEKNORA_BASE_URL = os.getenv("WEKNORA_BASE_URL", "http://localhost:8080")
WEKNORA_API_KEY  = os.getenv("WEKNORA_API_KEY", "")

REPARSE_QUEUE = pathlib.Path("data/weknora_reparse_queue.txt")
REPARSE_DONE  = pathlib.Path("data/weknora_reparse_done.txt")

REQUEST_DELAY = 0.2


def ping():
    resp = httpx.get(
        f"{WEKNORA_BASE_URL.rstrip('/')}/api/v1/knowledge-bases",
        headers={"X-API-Key": WEKNORA_API_KEY},
        timeout=10,
    )
    if resp.status_code == 401:
        raise RuntimeError("API Key 无效")
    resp.raise_for_status()


def reparse(knowledge_id: str):
    resp = httpx.post(
        f"{WEKNORA_BASE_URL.rstrip('/')}/api/v1/knowledge/{knowledge_id}/reparse",
        headers={"X-API-Key": WEKNORA_API_KEY},
        timeout=30,
    )
    resp.raise_for_status()


def main(dry_run: bool):
    if not WEKNORA_API_KEY:
        logger.error("未配置 WEKNORA_API_KEY")
        sys.exit(1)

    try:
        ping()
        logger.info(f"连通性检查通过: {WEKNORA_BASE_URL}")
    except Exception as e:
        logger.error(f"连通性检查失败: {e}")
        sys.exit(1)

    if not REPARSE_QUEUE.exists():
        logger.info("队列文件不存在，无需处理")
        return

    # 启动时快照
    lines = REPARSE_QUEUE.read_text(encoding="utf-8").splitlines()
    todo = [l.strip() for l in lines if l.strip()]

    # 加载已完成的，跳过
    done = set()
    if REPARSE_DONE.exists():
        done = {l.strip() for l in REPARSE_DONE.read_text(encoding="utf-8").splitlines() if l.strip()}

    todo = [kid for kid in todo if kid not in done]
    logger.info(f"队列共 {len(todo)} 条待触发索引")

    if not todo:
        logger.info("无待处理记录，退出")
        return

    success = failed = 0
    failed_ids = []

    for kid in todo:
        if dry_run:
            logger.info(f"[DryRun] reparse {kid}")
            continue
        try:
            reparse(kid)
            with REPARSE_DONE.open("a", encoding="utf-8") as f:
                f.write(kid + "\n")
            logger.info(f"✓ {kid}")
            success += 1
        except Exception as e:
            logger.error(f"✗ {kid}: {e}")
            failed_ids.append(kid)
            failed += 1
        time.sleep(REQUEST_DELAY)

    if not dry_run:
        # 失败的写回队列（覆盖，只保留失败的）
        remaining = [l.strip() for l in REPARSE_QUEUE.read_text(encoding="utf-8").splitlines() if l.strip()]
        remaining = [kid for kid in remaining if kid in set(failed_ids)]
        REPARSE_QUEUE.write_text("\n".join(remaining) + ("\n" if remaining else ""), encoding="utf-8")
        logger.info(f"完成 | 成功={success} 失败={failed}")


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")

    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="预览，不实际触发")
    args = parser.parse_args()

    main(dry_run=args.dry_run)
