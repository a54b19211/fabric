"""Derive the V021 ranking keys of versions written before it, a small batch at a time, one source after
another. Idempotent and resumable; history_versions is only read. Prints counts only. Run with the history
service's environment:

  python -m ops.backfill_history_rank europe_pmc clinicaltrials who_don fda_crl [--batch 500]
"""
import argparse
import time

from store.history import backfill_rank


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("sources", nargs="+")
    parser.add_argument("--batch", type=int, default=500)
    parser.add_argument("--pause", type=float, default=0.2)
    args = parser.parse_args()
    for source in args.sources:
        total = 0
        while added := backfill_rank(source, batch=args.batch):
            total += added
            time.sleep(args.pause)
        print(source, "added", total, flush=True)


if __name__ == "__main__":
    main()
