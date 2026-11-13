"""T-30 scheduling on real PostgreSQL: exclusive job classes, wait bounds under backlog and continuous inflow,
round robin across sources, an atomic in-flight budget and a shared send bucket on every physical upload.
Synthetic jobs are clones of one real job (only their scheduling fields differ); no upstream network."""

import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from integration_index_publication import (  # noqa: F401  (fixtures)
    JournalClient,
    add_document,
    pg,
    pg_engine,
    ready_job,
    receipt_for,
    rows,
    scan,
)
from sqlalchemy import text

from store import index_publication as index

KB = "synthetic-isolated-kb"


def columns(pg, table):
    with pg.begin() as conn:
        return [r[0] for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name=:t AND table_schema=current_schema() "
            "ORDER BY ordinal_position"), {"t": table})]


def clone(pg, base, n=1, *, target_id=None, **fields):
    """n copies of a job row with new identities; `fields` are column values (SQL literals as text)."""
    cols = columns(pg, "index_publications")
    ids = []
    with pg.begin() as conn:
        for _ in range(n):
            new = uuid4().hex + uuid4().hex  # ids are char(64)
            over = {"id": f"'{new}'", "remote_id": f"'{new}'", "piece_key": f"'clone:{new}'",
                    "source_record_id": f"'clone-{new}'", "lease_token": "NULL", "lease_until": "NULL",
                    "next_check_at": "clock_timestamp()", **({"target_id": f"'{target_id}'"} if target_id else {}),
                    **fields}
            select = ",".join(over.get(c, c) for c in cols)
            conn.execute(text(f"INSERT INTO index_publications ({','.join(cols)}) SELECT {select} "
                              "FROM index_publications WHERE id=:id"), {"id": base["id"]})
            ids.append(new)
    return ids


def second_target(pg, base, source_id="clinicaltrials"):
    cols = columns(pg, "index_targets")
    new = uuid4().hex + uuid4().hex
    with pg.begin() as conn:
        config = conn.execute(text("SELECT config FROM index_targets WHERE id=:id"), {"id": base["target_id"]}).scalar_one()
        config = dict(config, source_id=source_id)
        over = {"id": f"'{new}'", "config": "CAST(:config AS jsonb)"}
        select = ",".join(over.get(c, c) for c in cols)
        conn.execute(text(f"INSERT INTO index_targets ({','.join(cols)}) SELECT {select} FROM index_targets WHERE id=:id"),
                     {"id": base["target_id"], "config": json.dumps(config)})
    return new


def class_of(pg, job_id):
    with pg.begin() as conn:
        return conn.execute(text("SELECT " + index.CLASS_OF + " FROM index_publications p WHERE p.id=:id"),
                            {"id": job_id}).scalar_one()


def handled(pg, row):
    """What processing would leave behind for the claim simulation: lease released, checked again later."""
    with pg.begin() as conn:
        conn.execute(text("UPDATE index_publications SET lease_token=NULL,lease_until=NULL,"
                          "next_check_at=clock_timestamp()+interval '1 day' WHERE id=:id"), {"id": row["id"]})


def turn(n, phase=0):
    k = (n + phase) % len(index.ROTATION)
    for klass in dict.fromkeys(index.ROTATION[k:] + index.ROTATION[:k]):
        row = index._claim_class(klass)
        if row is not None:
            return row
    return None


def base_job(pg):
    """One real job, uploaded and ready. Its upload took a send token like any other: the bucket and gate
    counters start empty again for the test."""
    version, _ = ready_job(pg)
    (row,) = rows(pg, "SELECT * FROM index_publications WHERE version_id=:v", {"v": version})
    with pg.begin() as conn:
        conn.execute(text("DELETE FROM index_send_bucket"))
        conn.execute(text("DELETE FROM index_gate_stats"))
    return row


SENT = {"first_send_at": "clock_timestamp()"}
UNSENT = {"first_send_at": "NULL", "knowledge_id": "NULL", "receipt": "'{}'::jsonb"}


def test_job_classes_are_exclusive_and_cover_every_live_state(pg):
    base = base_job(pg)
    cases = [
        ({"state": "'disabling'", "desired_state": "'superseded'"}, "R"),
        ({"state": "'ready'", "desired_state": "'revoked'"}, "R"),
        ({"state": "'blocked'", "desired_state": "'retiring'"}, "R"),
        ({"state": "'indexing'", "desired_state": "'active'", **SENT}, "P"),
        ({"state": "'waiting'", "desired_state": "'active'", **SENT}, "P"),           # sent, outcome open
        ({"state": "'queued'", "desired_state": "'active'", "receipt": "jsonb_build_object('x',1)"}, "P"),  # has a receipt
        ({"state": "'queued'", "desired_state": "'active'", "lane": "'new'", **UNSENT}, "F"),
        ({"state": "'waiting'", "desired_state": "'active'", "lane": "'new'", **UNSENT}, "F"),  # failed before send
        ({"state": "'queued'", "desired_state": "'active'", "lane": "'backfill'", **UNSENT}, "B"),
        ({"state": "'ready'", "desired_state": "'active'"}, "C"),
        ({"state": "'blocked'", "desired_state": "'active'", **SENT}, "C"),
        ({"state": "'disabled'", "desired_state": "'superseded'"}, "C"),
    ]
    for fields, expected in cases:
        (job_id,) = clone(pg, base, **fields)
        assert class_of(pg, job_id) == expected, (fields, class_of(pg, job_id))
    # exclusive by construction: each predicate excludes R, P and F/B exclude each other by send evidence/lane
    with pg.begin() as conn:
        overlaps = conn.execute(text(
            "SELECT count(*) FROM index_publications p WHERE (" + "::int+".join(
                "(" + index.CLASS_SQL[k] + ")" for k in index.CLASS_SQL) + "::int)>1")).scalar_one()
    assert overlaps == 0


def test_every_class_is_served_within_its_bound_under_backlog_and_inflow(pg, monkeypatch):
    """The 3.6 starvation (538 due rows ahead of 13 targets) without restricting the claim to any ID: each due
    job is claimed within (a+1) * S * ceil(8/q) claims, with a fresh first upload added after every claim.
    (The claim order alone: the in-flight budget is opened wide here, it has tests of its own.)"""
    monkeypatch.setenv("DATAFABRIC_INDEX_MAX_INFLIGHT", "100000")
    base = base_job(pg)
    with pg.begin() as conn:
        conn.execute(text("UPDATE index_publications SET next_check_at=clock_timestamp()+interval '1 day'"))
    polls = clone(pg, base, 538, state="'indexing'", desired_state="'active'", **SENT)
    firsts = clone(pg, base, 13, state="'queued'", desired_state="'active'", lane="'new'", **UNSENT)
    retires = clone(pg, base, 5, state="'disabling'", desired_state="'superseded'")
    rechecks = clone(pg, base, 5, state="'ready'", desired_state="'active'")
    backfills = clone(pg, base, 4, state="'queued'", desired_state="'active'", lane="'backfill'", **UNSENT)
    claimed, started = {}, time.monotonic()
    for n in range(200):
        row = turn(n)
        assert row is not None
        claimed.setdefault(row["id"], (n + 1, row["job_class"], time.monotonic() - started))
        handled(pg, row)
        clone(pg, base, state="'queued'", desired_state="'active'", lane="'new'", **UNSENT)  # continuous inflow
    bound = index.rotation_gaps()  # worst phase: F 5, P 3, R / B / C 8
    assert bound == {"F": 5, "P": 3, "R": 8, "B": 8, "C": 8}
    for group, klass in ((firsts, "F"), (retires, "R"), (rechecks, "C"), (backfills, "B"), (polls[:60], "P")):
        for a, job_id in enumerate(group):
            assert job_id in claimed, (klass, a)
            n, got, _ = claimed[job_id]
            assert got == klass and n <= (a + 1) * bound[klass], (klass, a, n)
    served = {k: sum(1 for _, c, _ in claimed.values() if c == k) for k in bound}
    assert served["P"] >= 200 * 3 // 8 - 1 and served["R"] == 5 and served["C"] == 5 and served["B"] == 4


def test_the_bound_holds_from_every_starting_phase(pg, monkeypatch):
    monkeypatch.setenv("DATAFABRIC_INDEX_MAX_INFLIGHT", "100000")
    base = base_job(pg)
    bound = index.rotation_gaps()
    for phase in range(len(index.ROTATION)):
        with pg.begin() as conn:
            conn.execute(text("UPDATE index_publications SET next_check_at=clock_timestamp()+interval '1 day'"))
            conn.execute(text("DELETE FROM index_claim_cursor"))
        groups = {"P": clone(pg, base, 12, state="'indexing'", desired_state="'active'", **SENT),
                  "F": clone(pg, base, 4, state="'queued'", desired_state="'active'", lane="'new'", **UNSENT),
                  "R": clone(pg, base, 2, state="'disabling'", desired_state="'superseded'"),
                  "C": clone(pg, base, 2, state="'ready'", desired_state="'active'"),
                  "B": clone(pg, base, 2, state="'queued'", desired_state="'active'", lane="'backfill'", **UNSENT)}
        claimed = {}
        for n in range(40):
            row = turn(n, phase)
            claimed.setdefault(row["id"], n + 1)
            handled(pg, row)
            clone(pg, base, state="'indexing'", desired_state="'active'", **SENT)  # other classes keep busy
        for klass, ids in groups.items():
            for a, job_id in enumerate(ids):
                assert claimed.get(job_id, 10 ** 6) <= (a + 1) * bound[klass], (phase, klass, a, claimed.get(job_id))


def test_sources_take_turns_inside_a_class(pg):
    base = base_job(pg)
    with pg.begin() as conn:
        conn.execute(text("UPDATE index_publications SET next_check_at=clock_timestamp()+interval '1 day'"))
    other = second_target(pg, base)
    many = clone(pg, base, 50, state="'indexing'", desired_state="'active'", **SENT)
    few = clone(pg, base, 3, target_id=other, state="'indexing'", desired_state="'active'", **SENT)
    order = []
    while len(order) < 12:
        row = index._claim_class("P")
        order.append(row["id"])
        handled(pg, row)
    for a, job_id in enumerate(few):
        assert order.index(job_id) + 1 <= (a + 1) * 2  # S = 2 sources
    assert set(order[:6]) & set(many) and set(order[:6]) >= set(few)


def lease_all(pg, ids):
    with pg.begin() as conn:
        conn.execute(text("UPDATE index_publications SET lease_token=id,lease_until=clock_timestamp()+interval '5 min' "
                          "WHERE id=ANY(:ids)"), {"ids": ids})
    return rows(pg, "SELECT * FROM index_publications WHERE id=ANY(:ids)", {"ids": ids})


def test_inflight_budget_is_atomic_across_workers_and_only_settled_uploads_release(pg, monkeypatch):
    monkeypatch.setenv("DATAFABRIC_INDEX_MAX_INFLIGHT", "5")
    monkeypatch.setenv("DATAFABRIC_INDEX_UPLOAD_BURST", "1000")
    monkeypatch.setenv("DATAFABRIC_INDEX_UPLOADS_PER_MINUTE", "100000")
    base = base_job(pg)
    jobs = lease_all(pg, clone(pg, base, 20, state="'queued'", desired_state="'active'", lane="'new'", **UNSENT))
    with ThreadPoolExecutor(8) as pool:
        outcomes = list(pool.map(lambda r: index._reserve(r, "fixed text " * 10), jobs))
    granted = [j for j, o in zip(jobs, outcomes) if o is None]
    assert len(granted) == 5 and set(o for o in outcomes if o) == {"inflight_entries"}
    with pg.begin() as conn:
        def count():
            return index._inflight(conn, KB)[0]
        assert count() == 5, rows(pg, "SELECT state,first_send_at IS NOT NULL AS sent,receipt,knowledge_id "
                                      "FROM index_publications WHERE state<>'queued'")
        g = [j["id"] for j in granted]
        # blocked or waiting with an open remote outcome keeps its slot
        conn.execute(text("UPDATE index_publications SET state='blocked',receipt='{\"parse_status\":\"processing\"}' WHERE id=:id"), {"id": g[0]})
        conn.execute(text("UPDATE index_publications SET state='waiting',lease_until=clock_timestamp()-interval '1 s' WHERE id=:id"), {"id": g[1]})
        assert count() == 5, [dict(r) for r in conn.execute(text(
            "SELECT state,first_send_at IS NOT NULL AS sent,receipt,knowledge_id,manifest->>'knowledge_base_id' AS kb "
            "FROM index_publications WHERE id=ANY(:ids)"), {"ids": g}).mappings()]
        # a rejection or an ended parse releases it only when the receipt speaks of this attempt (a later one
        # than the attempt known when it was reserved, attempt_floor 0 here)
        conn.execute(text("UPDATE index_publications SET state='waiting',knowledge_id=NULL,"
                          "receipt='{\"upload_status\":\"rejected\"}' WHERE id=:id"), {"id": g[2]})
        conn.execute(text("UPDATE index_publications SET state='blocked',receipt='{\"parse_status\":\"completed\"}' WHERE id=:id"), {"id": g[3]})
        assert count() == 5
        conn.execute(text("UPDATE index_publications SET receipt=receipt||jsonb_build_object('last_attempt',"
                          "jsonb_build_object('attempt',1,'outcome','rejected')) WHERE id=:id"), {"id": g[2]})
        conn.execute(text("UPDATE index_publications SET receipt=receipt||jsonb_build_object('last_attempt',"
                          "jsonb_build_object('attempt',1,'outcome','uploaded')) WHERE id=:id"), {"id": g[3]})
        assert count() == 3
        # a later attempt that is still in flight or unknown proves nothing (Codex r111)
        for outcome in ("in_flight", "unknown"):
            conn.execute(text("UPDATE index_publications SET receipt=receipt||jsonb_build_object('last_attempt',"
                              "jsonb_build_object('attempt',2,'outcome',CAST(:o AS text))) WHERE id=:id"),
                         {"id": g[2], "o": outcome})
            assert count() == 4, outcome
        conn.execute(text("UPDATE index_publications SET receipt=receipt||jsonb_build_object('last_attempt',"
                          "jsonb_build_object('attempt',2,'outcome','rejected')) WHERE id=:id"), {"id": g[2]})
        assert count() == 3
        sizes = conn.execute(text("SELECT sent_chars,sent_bytes FROM index_publications WHERE id=:id"), {"id": g[4]}).one()
    assert tuple(sizes) == (len("fixed text " * 10), len(("fixed text " * 10).encode()))


def test_the_send_bucket_is_shared_and_counts_every_physical_upload(pg, monkeypatch):
    monkeypatch.setenv("DATAFABRIC_INDEX_UPLOAD_BURST", "3")
    monkeypatch.setenv("DATAFABRIC_INDEX_UPLOADS_PER_MINUTE", "0.0001")
    base = base_job(pg)
    jobs = lease_all(pg, clone(pg, base, 10, state="'queued'", desired_state="'active'", lane="'new'", **UNSENT))
    with ThreadPoolExecutor(5) as pool:
        outcomes = list(pool.map(lambda r: index._reserve(r, "x"), jobs))
    assert outcomes.count(None) == 3 and outcomes.count("send_rate") == 7
    gate = rows(pg, "SELECT denials FROM index_gate_stats WHERE knowledge_base_id=:kb AND reason='send_rate'", {"kb": KB})
    assert gate == [{"denials": 7}]
    status = index.scheduler_status()
    (kb,) = [k for k in status["knowledge_bases"] if k["knowledge_base_id"] == KB]
    assert kb["inflight"]["entries"] == 3 and kb["gate"]["send_rate"]["denials"] == 7 and kb["send_tokens"] < 1


class RetryClient(JournalClient):
    """A bridge that recorded the first upload as rejected before creation: the next POST is a real upload."""

    def publish(self, body):
        pid = next(iter(self.journal)) if self.journal else None
        if pid and self.journal[pid].get("upload_status") == "rejected":
            self.journal.pop(pid)
        return super().publish(body)


def test_a_rejected_retry_in_the_poll_class_passes_the_gate_and_binding_does_not(pg, monkeypatch):
    monkeypatch.setenv("DATAFABRIC_INDEX_UPLOAD_BURST", "1")
    monkeypatch.setenv("DATAFABRIC_INDEX_UPLOADS_PER_MINUTE", "0.0001")
    add_document()
    scan()
    (row,) = rows(pg, "SELECT * FROM index_publications")
    client = RetryClient()
    rejected = {"publication_id": row["remote_id"], "upload_status": "rejected", "knowledge_id": None}
    client.journal[row["remote_id"]] = rejected
    with pg.begin() as conn:
        conn.execute(text("UPDATE index_publications SET first_send_at=clock_timestamp(),state='waiting',"
                          "receipt=CAST(:r AS jsonb) WHERE id=:id"), {"r": json.dumps(rejected), "id": row["id"]})
        conn.execute(text("INSERT INTO index_send_bucket VALUES (:kb,0,clock_timestamp()) "
                          "ON CONFLICT (knowledge_base_id) DO UPDATE SET tokens=0,refilled_at=clock_timestamp()"), {"kb": KB})
    monkeypatch.setattr(index, "_validate_receipt", lambda row, receipt: None)  # the stub receipt has no identity
    assert class_of(pg, row["id"]) == "P"
    assert index.process_once(client=client, classes=["P"])["state"] == "waiting"  # held, nothing sent
    assert client.upload_count == 0 and ("post", row["remote_id"]) not in client.events
    assert rows(pg, "SELECT denials FROM index_gate_stats WHERE reason='send_rate'") == [{"denials": 1}]
    with pg.begin() as conn:
        conn.execute(text("UPDATE index_send_bucket SET tokens=1"))
        conn.execute(text("UPDATE index_publications SET next_check_at=clock_timestamp()"))
    assert index.process_once(client=client, classes=["P"])["state"] == "ready"
    assert client.upload_count == 1
    # binding only (the bridge already has the knowledge): no token is needed and none is taken
    with pg.begin() as conn:
        conn.execute(text("UPDATE index_send_bucket SET tokens=0,refilled_at=clock_timestamp()"))
        conn.execute(text("UPDATE index_publications SET next_check_at=clock_timestamp()"))
    client.journal[row["remote_id"]]["binding_status"] = "pending"
    assert index.process_once(client=client, classes=["C"])["state"] in ("ready", "indexing")
    assert client.upload_count == 1
    assert rows(pg, "SELECT denials FROM index_gate_stats WHERE reason='send_rate'") == [{"denials": 1}]


def test_a_sending_job_after_a_restart_is_polled_not_uploaded_again(pg, monkeypatch):
    monkeypatch.setenv("DATAFABRIC_INDEX_UPLOAD_BURST", "1")
    monkeypatch.setenv("DATAFABRIC_INDEX_UPLOADS_PER_MINUTE", "0.0001")
    add_document()
    scan()
    (row,) = rows(pg, "SELECT * FROM index_publications")
    client = JournalClient()
    body_receipt = None
    # the upload reached the bridge, then the worker died before saving the receipt
    with pg.begin() as conn:
        conn.execute(text("UPDATE index_publications SET first_send_at=clock_timestamp(),state='sending',"
                          "lease_token='dead',lease_until=clock_timestamp()-interval '1 s' WHERE id=:id"), {"id": row["id"]})
    first = JournalClient()
    index.process_once(client=first, classes=["P"])  # learns the body the worker would send
    body_receipt = copy.deepcopy(first.journal)
    client.journal = body_receipt
    with pg.begin() as conn:
        conn.execute(text("UPDATE index_publications SET state='sending',next_check_at=clock_timestamp()"))
        conn.execute(text("INSERT INTO index_send_bucket VALUES (:kb,0,clock_timestamp()) "
                          "ON CONFLICT (knowledge_base_id) DO UPDATE SET tokens=0,refilled_at=clock_timestamp()"), {"kb": KB})
        conn.execute(text("DELETE FROM index_gate_stats"))
    assert class_of(pg, row["id"]) == "P"
    assert index.process_once(client=client, classes=["P"])["state"] == "ready"
    assert client.upload_count == 0 and rows(pg, "SELECT count(*) AS n FROM index_gate_stats") == [{"n": 0}]


def test_migration_recovers_the_size_of_earlier_uploads(pg):
    from store.migrations import runner

    base = base_job(pg)
    (piece,) = clone(pg, base, state="'indexing'", desired_state="'active'", **SENT,
                     piece="jsonb_build_object('index',1,'count',2,'chars',123,'bytes',456)",
                     sent_chars="NULL", sent_bytes="NULL")
    with pg.begin() as conn:
        conn.execute(text("UPDATE index_publications SET sent_chars=NULL,sent_bytes=NULL WHERE id=:id"), {"id": base["id"]})
        text_row = conn.execute(text("SELECT length(text),octet_length(text) FROM derived_clean_text WHERE version_id=:v "
                                     "AND processor_version||':'||text_sha256=:k"),
                                {"v": base["version_id"], "k": base["artifact_key"]}).first()
    with pg.begin() as conn:  # a store from before V031 has no record of it: its file runs on it
        conn.exec_driver_sql((runner.VERSIONS / "V031__index_scheduling.sql")
                             .read_text(encoding="utf-8"))
    got = {r["id"]: (r["sent_chars"], r["sent_bytes"]) for r in rows(pg, "SELECT id,sent_chars,sent_bytes FROM index_publications")}
    assert got[piece] == (123, 456)
    assert got[base["id"]] == tuple(text_row)


def test_an_upload_of_unknown_size_blocks_new_uploads_until_known(pg, monkeypatch):
    base = base_job(pg)
    (legacy,) = clone(pg, base, state="'indexing'", desired_state="'active'", receipt="'{}'::jsonb",
                      sent_chars="NULL", sent_bytes="NULL", **SENT)
    candidate = lease_all(pg, clone(pg, base, state="'queued'", desired_state="'active'", lane="'new'", **UNSENT))[0]
    assert index._reserve(candidate, "x") == "inflight_size_unknown"
    status = index.scheduler_status()
    (kb,) = [k for k in status["knowledge_bases"] if k["knowledge_base_id"] == KB]
    assert kb["inflight"]["unknown_size_entries"] == 1 and kb["gate"]["inflight_size_unknown"]["denials"] == 1
    with pg.begin() as conn:
        conn.execute(text("UPDATE index_publications SET sent_chars=10,sent_bytes=10 WHERE id=:id"), {"id": legacy})
    assert index._reserve(candidate, "x") is None


def test_uploaded_characters_are_counted_with_the_re_sends_and_entries_without_a_size_are_named(pg):
    base = base_job(pg)

    def uploaded():
        (kb,) = [k for k in index.scheduler_status()["knowledge_bases"] if k["knowledge_base_id"] == KB]
        return kb["uploaded"]
    once = uploaded()  # the real upload that made the base job ready
    assert (once["uploads"], once["chars"], once["retried_uploads"]) == (1, base["sent_chars"], 0)
    clone(pg, base, state="'ready'", desired_state="'active'", sent_chars="1000", sent_bytes="1000", attempt_floor="0", **SENT)
    clone(pg, base, state="'ready'", desired_state="'active'", sent_chars="400", sent_bytes="400", attempt_floor="2", **SENT)
    clone(pg, base, state="'indexing'", desired_state="'active'", receipt="'{}'::jsonb", sent_chars="NULL", sent_bytes="NULL", **SENT)
    clone(pg, base, state="'queued'", desired_state="'active'", lane="'new'", **UNSENT)  # never sent: not an upload
    assert uploaded() == {
        "uploads": 1 + 1 + 3,  # the base job, one upload, one entry sent a third time
        "chars": once["chars"] + 1000 + 3 * 400, "retried_uploads": 2, "retried_chars": 800,
        "unsized_entries": 1, "embedding_requests": "unknown"}
