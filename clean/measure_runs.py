"""测量的运行：给每份新的清洗正文排运行，一个窗口一个窗口地读，读到的数字直接写成测量对象。

从哪读起：plan() 排运行，process_once() 读一个窗口。
- 运行（derived_measure_runs）：一条路线读一个版本的一份清洗正文一次，run_id = sha256(版本|正文哈希|producer)。
  同一份正文已经被同一条路线读过就不再排（换 producer 版本不会自动重读旧正文）；清洗器升级出了新正文才排新运行。
  回填进来的背景版本（store.history.background_version_ids）不排。
- 窗口（derived_measure_windows）就是待办：process_once() 用 SELECT ... FOR UPDATE SKIP LOCKED 认领一个到期窗口，
  在同一个事务里读完、写完；进程中断时锁随事务释放，窗口留给下一轮。读失败的窗口退避重试，失败 3 次是 failed_final
  （运行记 partial）；模型说“现在不行”（429/503）的窗口记 budget_spent，稍后再读，不算失败。
  每次读都在 derived_measure_window_attempts 记一行：模型原始输出、读到几个、没成读数的原因。
- 过了底线的读数（clean.measure_extract.reading_of）直接成为测量对象：derived_measure_readings 一行（item 列是读数，
  passages、final 两列是核验时代留下的，写空）、derived_measurements 一行（check 一律 unchecked，照样往下流），
  再记一条 created 事件（derived_measurement_events，变更流按 published_seq）。
  reading_id 和 measurement_id 是同一个值：sha256(运行|读数内容)，同样的读数写两次也只有一个对象。
- 一个运行的窗口都读完后，同一版本、同一路线更早运行的对象被它取代：superseded_by_processor（正文变了）或
  superseded_by_producer（正文没变），指向新运行里引文、指标、数值相同的那个对象（没有就为空）。
"""

from __future__ import annotations

import json

import httpx
from sqlalchemy import text

from clean import measure_extract as mx
from store import history
from store.history_models import HistoryError

MAX_ATTEMPTS = 3
CONTRACT = "datafabric.data_objects.v2"  # the shape of a measurement object, as the API serves it
UNCHECKED = mx.UNCHECKED  # no checker reads measurements: checker_version, check contract and check.status say so
SUPERSEDING = ("superseded_by_processor", "superseded_by_producer", "rechecked_by_checker")  # the last: older rows
TO_READ = ("pending", "failed", "budget_spent")


def readable(conn, source_id) -> bool:
    """Whether the source may be read now (the history permission: allowlist and read switch)."""
    try:
        history._source(conn, source_id)
        return True
    except HistoryError as exc:
        if exc.status != 403:
            raise
        return False


def routes(model, with_rules):
    """[(route, producer identity, configuration stored with the run)] this process reads with."""
    out = [(mx.MODEL, mx.model_identity(model), model.config)] if model else []
    if with_rules:
        out.append((mx.RULES, mx.RULES_IDENTITY, {"route": mx.RULES, "method": mx.who_facts.METHOD}))
    return out


def _sources(conn):
    from clean import processors

    return [s for s in processors.MEASURE_SOURCES if readable(conn, s)]


# ---- planning ------------------------------------------------------------------------------------------------------

# the current clean texts of the measured sources that no run of the route has read
_CANDIDATES = """
    SELECT DISTINCT ON (c.version_id) c.version_id, c.processor_version, c.text_sha256, c.text, v.source_id,
           v.source_record_id, v.is_demo, COALESCE(v.payload->>'raw_sha256', '') AS raw_sha256
      FROM derived_clean_text c JOIN history_versions v ON v.version_id = c.version_id
     WHERE c.processor_version = :tpv AND c.status = 'parsed' AND length(c.text) > 0 AND v.source_id = ANY(:sources)
       AND v.record_kind <> 'graph' AND v.version_id <> ALL(CAST(:background AS text[]))
       AND NOT EXISTS (SELECT 1 FROM derived_measure_runs r WHERE r.version_id = c.version_id
                         AND r.text_sha256 = c.text_sha256 AND (strpos(r.producer_identity, :mark) > 0) = :rules)
     ORDER BY c.version_id, c.attempt DESC
     LIMIT :n
"""


def plan(*, model=None, with_rules=True, batch=100) -> int:
    """A run, with its windows, for each current clean text no run of a route has read. Returns how many."""
    from clean import clean_text

    made = 0
    with history.history_engine().begin() as conn:
        params = {"tpv": clean_text.PROCESSOR_VERSION, "sources": _sources(conn), "mark": mx.RULES_MARK, "n": batch,
                  "background": history.background_version_ids(conn)}
        for route, identity, config in routes(model, with_rules):
            for row in conn.execute(text(_CANDIDATES), {**params, "rules": route == mx.RULES}).mappings().all():
                made += _plan_one(conn, dict(row), identity, config)
    return made


def _plan_one(conn, row, identity, config) -> int:
    run_id = mx.digest(row["version_id"].strip(), row["text_sha256"], identity)
    spans = mx.windows_of(row["text"])
    made = conn.execute(text("""
        INSERT INTO derived_measure_runs(run_id,version_id,source_id,source_record_id,is_demo,raw_sha256,
          text_processor_version,text_sha256,producer,producer_version,model_config_sha256,model_config,
          producer_identity,window_chars,overlap_chars,windows_total,state)
        VALUES (:r,:v,:s,:rec,:d,:raw,:tpv,:sha,:p,:pv,:cfg,CAST(:config AS jsonb),:pi,:wc,:oc,:n,'reading')
        ON CONFLICT DO NOTHING"""), {
        "r": run_id, "v": row["version_id"], "s": row["source_id"], "rec": row["source_record_id"],
        "d": row["is_demo"], "raw": row["raw_sha256"], "tpv": row["processor_version"], "sha": row["text_sha256"],
        "p": mx.PRODUCER, "pv": mx.PROCESSOR_VERSION, "config": json.dumps(config), "pi": identity,
        "cfg": None if mx.route_of(identity) == mx.RULES else mx.config_sha256(config),
        "wc": mx.WINDOW, "oc": mx.OVERLAP, "n": len(spans)}).rowcount
    if made:
        conn.execute(text("INSERT INTO derived_measure_windows(run_id,window_index,start_char,end_char,state) "
                          "VALUES (:r,:i,:a,:b,'pending')"),
                     [{"r": run_id, "i": i, "a": a, "b": b} for i, (a, b) in enumerate(spans)])
    return made


# ---- one window ----------------------------------------------------------------------------------------------------

_CLAIM = """
    SELECT w.run_id, w.window_index, w.start_char, w.end_char, w.attempts, r.version_id, r.source_id,
           r.source_record_id, r.is_demo, r.text_processor_version, r.text_sha256, r.producer_identity,
           v.first_seen_at, v.proof_available_at, v.payload->>'title' AS title
      FROM derived_measure_windows w JOIN derived_measure_runs r ON r.run_id = w.run_id
      JOIN history_versions v ON v.version_id = r.version_id
     WHERE r.state = 'reading' AND w.state IN ('pending','failed','budget_spent') AND w.next_at <= clock_timestamp()
       AND r.source_id = ANY(:sources) AND r.producer_identity = ANY(:identities)
     ORDER BY w.next_at, w.run_id, w.window_index
     LIMIT 1 FOR UPDATE OF w SKIP LOCKED
"""


def process_once(*, model=None, with_rules=True, client: httpx.Client | None = None) -> dict:
    """Read one due window of a run of this process's routes and write what it found, all in one transaction."""
    mine = routes(model, with_rules)
    if not mine:
        return {"state": "disabled"}
    with history.history_engine().begin() as conn:
        w = conn.execute(text(_CLAIM), {"sources": _sources(conn),
                                        "identities": [identity for _, identity, _ in mine]}).mappings().first()
        if w is None:
            return {"state": "idle"}
        w = dict(w)
        art = conn.execute(text(
            "SELECT text, segments FROM derived_clean_text WHERE version_id=:v AND processor_version=:tpv "
            "AND text_sha256=:sha AND status='parsed' ORDER BY attempt DESC LIMIT 1"),
            {"v": w["version_id"], "tpv": w["text_processor_version"], "sha": w["text_sha256"]}).mappings().first()
        if art is None:
            _attempt(conn, w, "artifact_changed")
            return {"state": _release(conn, w, "failed_final", "artifact_changed")}
        return {"state": _read(conn, w, art, model, client)}


def _measures(w, art, model, client):
    """(measures, skipped, output, error) the window's route read: the rules, or one model call."""
    window = (w["start_char"], w["end_char"])
    if mx.route_of(w["producer_identity"]) == mx.RULES:
        return (*mx.rules_measures(art["text"], art["segments"], window, w["source_record_id"]), None, None)
    owned = client is None
    client = client or httpx.Client(follow_redirects=False, trust_env=False)
    try:
        measures, output, error = mx.model_measures(model, w["title"], art["text"], window, client)
        return measures, [], output, error
    finally:
        if owned:
            client.close()


def _read(conn, w, art, model, client):
    """Read the window by its run's route, store the readings and objects, release the window; its new state."""
    asked = int(mx.route_of(w["producer_identity"]) == mx.MODEL)  # model requests sent for the window
    try:
        measures, skipped, output, error = _measures(w, art, model, client)
    except Exception as exc:  # noqa: BLE001 - a fault of a route fails its window (retried, then owed), not the worker
        measures, skipped, output, error = [], [], None, ("failed", f"route_error:{type(exc).__name__}")
    if error:
        _attempt(conn, w, error[0], error=error[1], output=output, requests=asked)
        return _release(conn, w, *error)
    ids, not_built = [], list(skipped)
    for n, m in enumerate(measures):
        reading, why = mx.reading_of(m, art["text"], (w["start_char"], w["end_char"]))
        if why:
            not_built.append({"measure": n, "reason": why})
        else:
            ids.append(_store(conn, w, reading, m))
    _attempt(conn, w, "sent", output=output, requests=asked, found=len(measures), reading_ids=ids,
             not_built=not_built)
    return _release(conn, w, "sent")


def _attempt(conn, w, outcome, *, error=None, output=None, requests=0, found=None, reading_ids=(), not_built=()):
    conn.execute(text("""
        INSERT INTO derived_measure_window_attempts(run_id,window_index,outcome,error,requests,output,measures_found,
          reading_ids,not_built)
        VALUES (:r,:i,:o,:e,:n,:out,:found,CAST(:ids AS jsonb),CAST(:nb AS jsonb))"""),
        {"r": w["run_id"], "i": w["window_index"], "o": outcome, "e": error, "n": requests, "out": output,
         "found": found, "ids": json.dumps(list(reading_ids)), "nb": json.dumps(list(not_built), ensure_ascii=False)})


def _release(conn, w, state, reason=None):
    """The window's new state; a failure counts an attempt and waits (5 minutes, doubling, at most an hour)."""
    attempts = w["attempts"] + (state == "failed")
    if state == "failed" and attempts >= MAX_ATTEMPTS:
        state = "failed_final"
    delay = min(3600, 300 * 2 ** w["attempts"]) if state in TO_READ else 0
    conn.execute(text("UPDATE derived_measure_windows SET state=:s,attempts=:a,reason=:why,updated_at=clock_timestamp(),"
                      "next_at=clock_timestamp()+make_interval(secs=>:d) WHERE run_id=:r AND window_index=:i"),
                 {"s": state, "a": attempts, "why": reason, "d": delay, "r": w["run_id"], "i": w["window_index"]})
    if state not in TO_READ:
        _settle(conn, w["run_id"])
    return state


# ---- objects and their events ----------------------------------------------------------------------------------------


def versions_of(obj) -> dict:
    """What produced an object (its events and receipts carry this)."""
    return {"data_objects": CONTRACT, "producer": obj["producer_identity"], "checker": obj["checker_version"],
            "contract": obj["contract_version"], "text_processor": obj["text_processor_version"]}


def body_of(w, reading) -> dict:
    """The measurement object of one reading of window w (its run and version columns), as the API serves it."""
    vid = w["version_id"].strip()
    return {"contract_version": CONTRACT,
            "inputs": [{"source_id": w["source_id"], "record_id": w["source_record_id"], "version_id": vid,
                        "role": "primary"}],
            "visibility": {"first_seen_at": w["first_seen_at"].isoformat(),
                           "proof_available_at": w["proof_available_at"] and w["proof_available_at"].isoformat()},
            **{k: v for k, v in reading.items() if k not in ("quote_start", "quote_end")},
            "check": {"status": UNCHECKED, "checker_version": UNCHECKED, "contract_version": UNCHECKED,
                      "run_id": w["run_id"]},
            "identity": {"state": "provisional",  # same name elsewhere is another key: never merged across records
                         "key": "literal:" + w["source_id"] + "|" + w["source_record_id"] + "|"
                                + " ".join(reading["object"].split()).casefold()}}


def _store(conn, w, reading, measure) -> str:
    """The reading and its measurement object (and its created event); returns the id they share."""
    mid = mx.digest(w["run_id"], mx.canonical(reading))
    conn.execute(text("""
        INSERT INTO derived_measure_readings(reading_id,run_id,window_index,version_id,source_id,is_demo,
          text_processor_version,text_sha256,producer_identity,quote_start,quote_end,item,passages,final,measure)
        VALUES (:id,:r,:wi,:v,:s,:d,:tpv,:sha,:pi,:q0,:q1,CAST(:item AS jsonb),'[]','{}',CAST(:m AS jsonb))
        ON CONFLICT DO NOTHING"""),
        {"id": mid, "r": w["run_id"], "wi": w["window_index"], "v": w["version_id"], "s": w["source_id"],
         "d": w["is_demo"], "tpv": w["text_processor_version"], "sha": w["text_sha256"], "pi": w["producer_identity"],
         "q0": reading["quote_start"], "q1": reading["quote_end"], "item": json.dumps(reading, ensure_ascii=False),
         "m": json.dumps(measure, ensure_ascii=False)})
    obj = conn.execute(text("""
        INSERT INTO derived_measurements(measurement_id,reading_id,run_id,version_id,source_id,source_record_id,
          is_demo,text_processor_version,text_sha256,producer_identity,checker_version,contract_version,check_run_id,
          check_row_id,body,first_seen_at,proof_available_at)
        VALUES (:m,:m,:r,:v,:s,:rec,:d,:tpv,:sha,:pi,:u,:u,:r,0,CAST(:body AS jsonb),:seen,:proof)
        ON CONFLICT DO NOTHING RETURNING *"""),
        {"m": mid, "r": w["run_id"], "v": w["version_id"], "s": w["source_id"], "rec": w["source_record_id"],
         "d": w["is_demo"], "tpv": w["text_processor_version"], "sha": w["text_sha256"], "pi": w["producer_identity"],
         "u": UNCHECKED, "body": json.dumps(body_of(w, reading), ensure_ascii=False), "seen": w["first_seen_at"],
         "proof": w["proof_available_at"]}).mappings().first()
    if obj is not None:
        event(conn, dict(obj), "created", "created", {})
    return mid


def event(conn, obj, kind, cause_key, cause, superseded_by=None) -> bool:
    """Append one event of an object (once per object, kind and cause). The feed number is taken under the feed
    row's lock, held to commit, so the feed order is the commit order."""
    if conn.execute(text("SELECT 1 FROM derived_measurement_events WHERE measurement_id=:m AND kind=:k "
                         "AND cause_key=:c"), {"m": obj["measurement_id"], "k": kind, "c": cause_key}).first():
        return False
    seq = conn.execute(text(
        "INSERT INTO derived_measurement_feed(is_demo,last_seq) VALUES (:d,1) ON CONFLICT (is_demo) "
        "DO UPDATE SET last_seq=derived_measurement_feed.last_seq+1 RETURNING last_seq"), {"d": obj["is_demo"]}).scalar_one()
    conn.execute(text("""
        INSERT INTO derived_measurement_events(is_demo,published_seq,measurement_id,source_id,version_id,kind,
          cause_key,cause,superseded_by,material_visible_at,material_proof_at,produced_at,effective_at,versions)
        SELECT :d,:seq,:m,:s,:v,:k,:ck,CAST(:cause AS jsonb),:by,:vis,:proof,now.t,now.t,CAST(:versions AS jsonb)
          FROM (SELECT clock_timestamp() AS t) now"""),
        {"d": obj["is_demo"], "seq": seq, "m": obj["measurement_id"], "s": obj["source_id"], "v": obj["version_id"],
         "k": kind, "ck": cause_key, "cause": json.dumps(cause), "by": superseded_by, "vis": obj["first_seen_at"],
         "proof": obj["proof_available_at"], "versions": json.dumps(versions_of(obj))})
    return True


# ---- the run ---------------------------------------------------------------------------------------------------------


def _settle(conn, run_id):
    """Once no window is left to read, the run is complete (or partial: windows failed for good) and supersedes the
    earlier runs of its version and route. The run row is locked first: two workers never both miss the end."""
    run = conn.execute(text("SELECT * FROM derived_measure_runs WHERE run_id=:r FOR UPDATE"),
                       {"r": run_id}).mappings().first()
    states = {r[0] for r in conn.execute(text("SELECT DISTINCT state FROM derived_measure_windows WHERE run_id=:r"),
                                         {"r": run_id})}
    if run["state"] != "reading" or states & set(TO_READ):
        return
    partial = "failed_final" in states
    conn.execute(text("UPDATE derived_measure_runs SET state=:s,reason=:why,updated_at=clock_timestamp() "
                      "WHERE run_id=:r"), {"s": "partial" if partial else "complete",
                                           "why": "windows_owed" if partial else None, "r": run_id})
    _supersede(conn, dict(run))


def _key(body):
    return body["quote"], body["metric"], body["value_text"]


def _supersede(conn, run):
    """The objects of the version's earlier runs of the same route, not superseded yet, are superseded by this run."""
    new = {_key(o["body"]): o["measurement_id"] for o in conn.execute(text(
        "SELECT measurement_id, body FROM derived_measurements WHERE run_id=:r"), {"r": run["run_id"]}).mappings()}
    olds = conn.execute(text("""
        SELECT m.* FROM derived_measurements m JOIN derived_measure_runs r ON r.run_id = m.run_id
         WHERE m.version_id = :v AND m.run_id <> :r AND r.created_at < :t
           AND (strpos(r.producer_identity, :mark) > 0) = :rules
           AND NOT EXISTS (SELECT 1 FROM derived_measurement_events e WHERE e.measurement_id = m.measurement_id
                             AND e.kind = ANY(:superseding))"""),
        {"v": run["version_id"], "r": run["run_id"], "t": run["created_at"], "mark": mx.RULES_MARK,
         "rules": mx.route_of(run["producer_identity"]) == mx.RULES, "superseding": list(SUPERSEDING)}).mappings()
    for old in [dict(o) for o in olds]:
        kind = "superseded_by_processor" if old["text_sha256"] != run["text_sha256"] else "superseded_by_producer"
        event(conn, old, kind, "run:" + run["run_id"], {"run_id": run["run_id"]}, new.get(_key(old["body"])))
