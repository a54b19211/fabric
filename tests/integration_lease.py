"""store.lease on real PostgreSQL: FOR UPDATE SKIP LOCKED, lease expiry and token ownership are database semantics,
so they are checked against the database, in a throwaway schema of the isolated test instance."""

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from store.lease import LeaseQueue, backoff, vacant

JOBS = LeaseQueue("lease_jobs", ("id",))
CLAIMS = LeaseQueue("lease_claims", ("version_id", "processor_version"))


@pytest.fixture
def pg():
    url = os.environ.get("DATAFABRIC_HISTORY_DATABASE_URL")
    if not url:
        pytest.skip("Explicit isolated PostgreSQL URL required")
    parsed = make_url(url)
    assert parsed.host == "127.0.0.1" and parsed.port == 18436 and parsed.database == "fabric_history"
    schema = "lease_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text("CREATE SCHEMA " + schema))
    engine = create_engine(url, connect_args={"options": "-csearch_path=" + schema})
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE lease_jobs (id int PRIMARY KEY, due boolean NOT NULL DEFAULT true, "
                          "state text NOT NULL DEFAULT 'queued', tries int NOT NULL DEFAULT 0, "
                          "lease_token text, lease_until timestamptz)"))
        conn.execute(text("INSERT INTO lease_jobs(id) SELECT generate_series(1, 3)"))
        conn.execute(text("CREATE TABLE lease_claims (version_id text, processor_version text, lease_token text, "
                          "lease_until timestamptz, PRIMARY KEY (version_id, processor_version))"))
    yield engine
    engine.dispose()
    with admin.begin() as conn:
        conn.execute(text("DROP SCHEMA " + schema + " CASCADE"))
    admin.dispose()


def claim(conn, **kw):
    return JOBS.claim(conn, lease_seconds=60, where="q.due", order_by="q.id", **kw)


def expire(pg, job_id):
    with pg.begin() as conn:
        conn.execute(text("UPDATE lease_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=:i"),
                     {"i": job_id})


def test_two_open_claims_take_different_jobs_without_waiting(pg):
    with pg.connect() as first, pg.connect() as second:
        a = first.begin()
        mine = claim(first)
        b = second.begin()
        theirs = claim(second)  # the first job is locked by the open transaction: skipped, not waited on
        assert (mine["id"], theirs["id"]) == (1, 2)
        assert mine["lease_token"] != theirs["lease_token"]
        a.commit()
        b.commit()


def test_a_live_lease_is_not_claimable_and_a_run_out_one_is(pg):
    with pg.begin() as conn:
        first = claim(conn)
    with pg.begin() as conn:
        assert claim(conn)["id"] == 2
    expire(pg, 1)
    with pg.begin() as conn:
        again = claim(conn)
    assert again["id"] == 1 and again["lease_token"] != first["lease_token"]


def test_only_the_current_owner_can_update_renew_or_release(pg):
    with pg.begin() as conn:
        first = claim(conn)
    expire(pg, 1)
    with pg.begin() as conn:
        current = claim(conn)
    with pg.begin() as conn:
        assert not JOBS.holds(conn, first) and JOBS.holds(conn, current)
        assert JOBS.update(conn, first, "state='done'") == 0
        assert JOBS.release(conn, first) == 0 and not JOBS.renew(conn, first, 60)
        assert JOBS.renew(conn, current, 120)
        assert JOBS.release(conn, current, "state='done',tries=tries+1") == 1
        row = conn.execute(text("SELECT state,tries,lease_token,lease_until FROM lease_jobs WHERE id=1")).one()
    assert tuple(row) == ("done", 1, None, None)


def test_token_only_ownership_survives_expiry_when_the_queue_asks_for_it(pg):
    with pg.begin() as conn:
        job = claim(conn)
    expire(pg, job["id"])
    with pg.begin() as conn:
        assert not JOBS.holds(conn, job) and JOBS.holds(conn, job, unexpired=False, lock=True)
        assert JOBS.update(conn, job, "state='late'") == 0
        assert JOBS.update(conn, job, "state='late'", unexpired=False, where_also="state='queued'") == 1


def test_claim_writes_extra_assignments_and_honours_a_state_based_vacancy(pg):
    with pg.begin() as conn:
        job = JOBS.claim(conn, lease_seconds=30, where="q.due AND q.tries<:max", order_by="q.id DESC",
                         params={"max": 1}, free="(q.state<>'running' OR q.lease_until<clock_timestamp())",
                         also_set="state='running',tries=tries+1", columns="q.id,q.tries")
        assert job == {"id": 3, "tries": 0, "lease_token": job["lease_token"]}
        row = conn.execute(text("SELECT state,tries,lease_until>clock_timestamp()+interval '25 seconds' "
                                "FROM lease_jobs WHERE id=3")).one()
    assert tuple(row) == ("running", 1, True)


def test_claims_table_form_inserts_once_and_reclaims_only_after_expiry(pg):
    key = {"version_id": "v1", "processor_version": "p1"}
    with pg.begin() as conn:
        first = CLAIMS.claim_key(conn, key, 60)
        assert first and CLAIMS.claim_key(conn, key, 60) is None
        assert CLAIMS.holds(conn, {**key, "lease_token": first})
        assert CLAIMS.update(conn, {**key, "lease_token": first}, "lease_until=clock_timestamp()",
                             unexpired=False) == 1
    with pg.begin() as conn:
        again = CLAIMS.claim_key(conn, key, 60)
    assert again not in (None, first)


def test_vacancy_sql_and_backoff():
    assert vacant("p") == "(p.lease_until IS NULL OR p.lease_until<clock_timestamp())"
    assert [backoff(n, base=10, cap=3600) for n in (0, 1, 6, 8, 20)] == [10, 20, 640, 2560, 2560]
    assert backoff(9, base=10, cap=3600, max_exponent=12) == 3600
