"""编号规则升级（clean/identifiers.py 的 PROCESSOR_VERSION）以后，给已经洗过的版本按新规则补编号。

    python ops/backfill_identifiers.py [--batch 500] [--after <version_id>]

清洗段只给还没画像的版本算编号。规则升级后，已画像的版本在新处理器版本下没有编号行，/clean-text 和索引元数据就读不到
它们的编号。这里按 version_id 的顺序把已画像的版本过一遍：取画像所基于的那份正文，按现在的规则抽取，写进
derived_identifiers（已有的不重复写；旧处理器版本的行保留）。一批一个事务，可以随时停，照最后打印的 version_id 用
--after 接着跑；清洗段不用停。
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from clean import identifiers, profile  # noqa: E402
from clean.clean_stage import INSERT_IDENTIFIERS  # noqa: E402
from store import records  # noqa: E402

# 一页已画像的版本和各自画像所基于的那份正文（同一处理器版本下最新一次成功结果）
PAGE = """
SELECT p.version_id, p.text_processor_version AS tpv, c.text
FROM (SELECT version_id, text_processor_version FROM derived_profiles
      WHERE processor_version=:prof AND version_id>:after ORDER BY version_id, text_processor_version LIMIT :n) p
LEFT JOIN LATERAL (SELECT text FROM derived_clean_text WHERE version_id=p.version_id
                   AND processor_version=p.text_processor_version AND status='parsed'
                   ORDER BY attempt DESC LIMIT 1) c ON true
ORDER BY p.version_id
"""


def id_rows(rows):
    """The derived_identifiers rows of one page of versions, under the current rules."""
    return [{"v": r["version_id"], "pv": identifiers.PROCESSOR_VERSION, "tpv": r["tpv"], "kind": hit["kind"],
             "value": hit["value"], "s": hit["start"], "e": hit["end"]}
            for r in rows if r["text"] for hit in identifiers.extract(r["text"])]


def backfill(batch: int, after: str) -> int:
    done = 0
    while True:
        with records.fabric_engine().begin() as conn:
            rows = conn.execute(text(PAGE), {"prof": profile.PROCESSOR_VERSION, "after": after, "n": batch}
                                ).mappings().all()
            if not rows:
                return done
            found = id_rows(rows)
            if found:
                conn.execute(text(INSERT_IDENTIFIERS), found)
        done, after = done + len(rows), rows[-1]["version_id"]
        if done % (batch * 20) == 0:
            print(f"{done} versions, after {after}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--batch", type=int, default=500)
    parser.add_argument("--after", default="")
    args = parser.parse_args()
    print(f"{backfill(args.batch, args.after)} versions done under {identifiers.PROCESSOR_VERSION}")
