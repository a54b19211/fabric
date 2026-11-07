import copy
import json
from sqlalchemy import text
from db import index_publication as index
from integration_index_scheduling import (KB,base_job,clone,lease_all,UNSENT,SENT)
from integration_index_scheduling import pg,pg_engine  # noqa: F401  (fixtures)
from integration_index_publication import ready_job,rows

def test_unknown_retry_cannot_reuse_a_previous_attempt_rejection_to_release_slot(pg,monkeypatch):
    monkeypatch.setenv("DATAFABRIC_INDEX_MAX_INFLIGHT","1")
    version,client=ready_job(pg)
    row=rows(pg,"SELECT * FROM index_publications WHERE version_id=:v",{"v":version})[0]
    rejected=copy.deepcopy(client.journal[row["remote_id"]])
    rejected.update(upload_status="rejected",knowledge_id=None,parse_status="not_started",index_status="pending",vector_status="pending",binding_status="pending")
    client.journal[row["remote_id"]]=rejected
    with pg.begin() as conn:
        conn.execute(text("UPDATE index_publications SET state='waiting',knowledge_id=NULL,receipt=CAST(:r AS jsonb),next_check_at=clock_timestamp() WHERE id=:id"),{"id":row["id"],"r":json.dumps(rejected)})
    events=[]
    def uncertain_retry(body):
        events.append("physical_post_accepted")
        accepted=copy.deepcopy(rejected)
        accepted.update(upload_status="uploaded",knowledge_id="accepted-on-retry",parse_status="processing")
        client.journal[row["remote_id"]]=accepted
        raise TimeoutError("response lost after upstream acceptance")
    client.publish=uncertain_retry
    assert index.process_once(client=client,classes=["P"])["state"]=="waiting"
    assert events==["physical_post_accepted"]
    with pg.begin() as conn:
        inflight=index._inflight(conn,KB)[0]
    assert inflight==1,rows(pg,"SELECT state,receipt,first_send_at,sent_chars FROM index_publications")

def test_preclaim_gate_skips_are_visible_in_capacity_statistics(pg,monkeypatch):
    monkeypatch.setenv("DATAFABRIC_INDEX_MAX_INFLIGHT","1")
    base=base_job(pg)
    clone(pg,base,state="'indexing'",desired_state="'active'",receipt="'{}'::jsonb",**SENT)
    clone(pg,base,state="'queued'",desired_state="'active'",lane="'new'",**UNSENT)
    assert index._claim_class("F") is None
    status=index.scheduler_status()
    kb=next(k for k in status["knowledge_bases"] if k["knowledge_base_id"]==KB)
    assert kb["inflight"]["entries"]==1
    assert kb["gate"],status

def test_migrated_unknown_sent_sizes_do_not_silently_become_zero_budget(pg,monkeypatch):
    monkeypatch.setenv("DATAFABRIC_INDEX_MAX_INFLIGHT_CHARS","100")
    monkeypatch.setenv("DATAFABRIC_INDEX_MAX_INFLIGHT_BYTES","100")
    base=base_job(pg)
    clone(pg,base,state="'indexing'",desired_state="'active'",receipt="'{}'::jsonb",sent_chars="NULL",sent_bytes="NULL",**SENT)
    candidate=lease_all(pg,clone(pg,base,state="'queued'",desired_state="'active'",lane="'new'",**UNSENT))[0]
    reason=index._reserve(candidate,"x"*100)
    assert reason is not None,"An unknown-size legacy upload plus a 100-byte new upload must not fit a 100-byte KB budget"
