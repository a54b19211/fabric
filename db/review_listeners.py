# -*- coding: utf-8 -*-
"""防护性事件监听 — review_status 应通过 repository 函数修改，不应直接赋值。"""

import warnings
import traceback

from sqlalchemy import event

from db.models import ExtractedEntity


@event.listens_for(ExtractedEntity.review_status, "set", active_history=True, propagate=True)
def _guard_direct_status_write(entity, new_value, old_value, _initiator):
    """检测绕过 repository 入口函数直写 review_status 的情况。"""
    if new_value == old_value:
        return

    for frame in traceback.extract_stack():
        if "review_lifecycle" in frame.filename or "repository" in frame.filename:
            return

    warnings.warn(
        f"[review_lifecycle] review_status 被直写绕过入口函数: "
        f"entity_id={entity.id}, "
        f"source_doc_uuid={entity.source_doc_uuid}, "
        f"{old_value} → {new_value}, "
        f"caller={traceback.extract_stack()[-3].filename}:{traceback.extract_stack()[-3].lineno}",
        stacklevel=2,
    )
