"""The original collection library's publication outbox (the common runner's `publication` role); only for an
explicitly configured library, never the object library by default.

    python -m migration.publication                      # = python -m migration.runner --db publication --apply
    python -m migration.publication --site-index         # = ... --online site-index
    python -m migration.publication --drop-site-index    # = ... --online site-index --drop
"""

import argparse

from db.publication import publication_engine
from migration import online, runner


def migrate_publication():
    return runner.apply("publication", publication_engine())


def site_index(*, drop=False):
    # an AUTOCOMMIT connection of its own: CONCURRENTLY does not run inside a migration transaction
    return online.run("publication", publication_engine(), "site-index", drop=drop)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--site-index", action="store_true")
    group.add_argument("--drop-site-index", action="store_true")
    args = parser.parse_args()
    if args.site_index or args.drop_site_index:
        site_index(drop=args.drop_site_index)
        print("独立来源游标索引操作完成")
    else:
        migrate_publication()
        print("原采集库发表待办及固定触发器已安装")
