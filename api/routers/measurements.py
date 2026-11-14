"""测量对象的两个读接口（雷达 series_sync 读，实现在 clean/measurement_reads.py）：一页版本的测量对象，和对象事件的
变更流。读者凭据和参数与 /clean-text 相同，每次读都查来源此刻的许可。"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from api.routers.fabric import _call, require_read
from clean.measurement_reads import changes, for_versions
from store.record_models import Mode

router = APIRouter(prefix="/api/fabric", tags=["资料库"])


@router.get("/measurements", dependencies=[Depends(require_read)])
def measurements_route(
    source_id: str,
    is_demo: bool,
    mode: Mode,
    cutoff: datetime,
    version_ids: Annotated[list[str], Query(min_length=1, max_length=100)],
    include_history: bool = False,
):
    """这些版本在截止时间可见的测量对象，每个带它的核验标记（check.status）和由事件折叠出的状态；
    默认只给当前的，include_history 也给已被取代的。"""
    return _call(for_versions, source_id, is_demo, version_ids, mode, cutoff, include_history=include_history)


@router.get("/measurements/changes", dependencies=[Depends(require_read)])
def measurement_changes_route(
    is_demo: bool,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    """after_seq 之后的对象事件（created、superseded_by_processor、superseded_by_producer），按提交顺序。"""
    return _call(changes, is_demo, after_seq=after_seq, limit=limit)
