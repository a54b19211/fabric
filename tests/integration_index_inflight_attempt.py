import json

from integration_index_scheduling import KB, SENT, base_job, clone, pg, pg_engine  # noqa: F401  (fixtures)

from db import index_publication as index


def test_a_new_inflight_attempt_is_not_proven_rejected_by_stale_receipt_state(pg):
    base=base_job(pg)
    receipt={"upload_status":"rejected","last_attempt":{"attempt":2,"outcome":"in_flight"}}
    clone(pg,base,state="'waiting'",desired_state="'active'",knowledge_id="NULL",attempt_floor="1",receipt="CAST('"+json.dumps(receipt)+"' AS jsonb)",**SENT)
    with pg.begin() as conn:
        assert index._inflight(conn,KB)[0]==1,"The new attempt is still in flight; the old rejection cannot release it"
