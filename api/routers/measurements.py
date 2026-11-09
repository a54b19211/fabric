"""The measurement objects of the processing layer (db.measurement_reads), their reading coverage
(db.measure_runs.coverage) and the change feed a consumer follows (Radar's series_sync). Read routes take the same
reader credential and parameters as /clean-text; every read checks the source permission at that moment."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from api.routers.history import BoundedHistoryRoute, _call, require_admin, require_read
from biz_rules.history_models import Mode

router = APIRouter(prefix="/api/history", tags=["历史资料"], route_class=BoundedHistoryRoute)


@router.get("/measurements", dependencies=[Depends(require_read)])
def measurements_route(
    source_id: str,
    is_demo: bool,
    mode: Mode,
    cutoff: datetime,
    version_ids: Annotated[list[str], Query(min_length=1, max_length=100)],
    include_history: bool = False,
):
    """The measurement objects of visible versions (datafabric.data_objects.v2), each with what its check found
    (check.status: consistent, inconsistent or unchecked) and its state at the cutoff folded from its events; by
    default only those current for the mode."""
    from db.measurement_reads import for_versions

    return _call(for_versions, source_id, is_demo, version_ids, mode, cutoff, include_history=include_history)


@router.get("/measurements/changes", dependencies=[Depends(require_read)])
def measurement_changes_route(
    is_demo: bool,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    """The events of measurement objects after a sequence number, in commit order (created, superseded, rechecked,
    corrected, retracted, correction withdrawn), with the versions that produced them."""
    from db.measurement_reads import changes

    return _call(changes, is_demo, after_seq=after_seq, limit=limit)


@router.get("/measurements/{measurement_id}", dependencies=[Depends(require_read)])
def measurement_route(measurement_id: str, is_demo: bool, mode: Mode, cutoff: datetime):
    """One fixed object, also when superseded or not current; 404 version_not_found when not visible at the cutoff."""
    from db.measurement_reads import read_one

    return _call(read_one, measurement_id, is_demo, mode, cutoff)


@router.get("/measure-coverage", dependencies=[Depends(require_read)])
def measure_coverage_route(
    source_id: str,
    is_demo: bool,
    mode: Mode,
    cutoff: datetime,
    version_ids: Annotated[list[str], Query(min_length=1, max_length=100)],
):
    """Per version and route: the reading run's state, the windows read, the windows its list left out and every owed
    window (partial = a coverage gap); where both routes read the same artifact, whether their readings agree."""
    from db.measure_runs import coverage

    return _call(coverage, source_id, is_demo, version_ids, mode, cutoff)


class IdentityBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    graph_source_id: str = Field(min_length=1, max_length=80)
    graph_record_id: str = Field(min_length=1, max_length=400)
    graph_version_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    namespace: str = Field(min_length=1, max_length=160)
    identity_id: str = Field(min_length=1, max_length=400)


@router.post("/measurements/{measurement_id}/identities", dependencies=[Depends(require_admin)])
def bind_identity_route(measurement_id: str, body: IdentityBinding):
    """Bind the object of a measurement to an identity confirmed in a fixed graph package whose evidence is this
    mention (409 otherwise: a name, alias or candidate is no identity)."""
    from db.measurement_identity import bind

    return _call(bind, measurement_id, **body.model_dump())
