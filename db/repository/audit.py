"""Operation and review audit-log writers shared by every repository module."""

from loguru import logger

from db.models import OperationLog, ReviewLog


def _write_operation_log(
    session,
    operation: str,
    target_table: str,
    target_id: int | None,
    business_action: str = "",
    description: str = "",
    details: dict | None = None,
    operator: str = "system",
    process: str = "",
    trigger_source: str = "",
) -> None:
    """写入一条操作日志。不抛异常，失败只记 warning。"""
    try:
        session.add(OperationLog(
            operation=operation,
            target_table=target_table,
            target_id=target_id,
            business_action=business_action,
            description=description,
            details=details or {},
            operator=operator,
            process=process,
            trigger_source=trigger_source,
        ))
    except Exception as e:
        logger.warning(f"[OperationLog] 写入失败: {e}")


def log_review_action(
    object_type: str,
    object_id: int,
    action: str,
    reviewer: str,
    note: str = "",
    before_data: dict = {},
    after_data: dict = {},
) -> None:
    """写入 review_log 审核操作记录。"""
    from db.session import get_session

    with get_session() as session:
        if session is None:
            return
        _write_review_log(
            session, object_type, object_id, action, reviewer, note,
            dict(before_data), dict(after_data),
        )
        _write_operation_log(
            session, "insert", "review_log", object_id,
            business_action="review",
            description=f"审核操作: {action} {object_type}={object_id} by {reviewer}",
            details={"object_type": object_type, "action": action,
                      "reviewer": reviewer, "note": note,
                      "before_data": dict(before_data), "after_data": dict(after_data)},
            operator=reviewer,
            trigger_source="log_review_action",
        )


def _write_review_log(
    session,
    object_type: str,
    object_id: int,
    action: str,
    reviewer: str,
    note: str,
    before_data: dict,
    after_data: dict,
) -> None:
    """在已有 session 中写 review_log，不自行 commit。"""
    session.add(ReviewLog(
        object_type=object_type,
        object_id=object_id,
        action=action,
        reviewer=reviewer,
        note=note,
        before_data=before_data,
        after_data=after_data,
    ))
