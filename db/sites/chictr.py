from db.models import ChictrMeta
from db.session import get_session


def db_writer(doc_uuid: str, parsed_result: dict) -> None:
    """将 chictr 专属字段写入 chictr_meta 表。"""
    extra = parsed_result.get("extra", {})
    if not extra:
        return

    with get_session() as session:
        if session is None:
            return

        values = dict(
            regno=extra.get("regno", ""),
            reg_status=extra.get("reg_status", ""),
            study_type=extra.get("study_type", ""),
            study_stage=extra.get("study_stage", ""),
            intervention=extra.get("intervention", ""),
            condition=extra.get("condition", ""),
            sample_size=extra.get("sample_size", ""),
            recruitment_status=extra.get("recruitment_status", ""),
            primary_sponsor=extra.get("primary_sponsor", ""),
            ethics_approved=extra.get("ethics_approved", ""),
            ethics_committee=extra.get("ethics_committee", ""),
            ethics_date=extra.get("ethics_date", ""),
            start_date=extra.get("start_date", ""),
            end_date=extra.get("end_date", ""),
        )

        existing = session.get(ChictrMeta, doc_uuid)
        if existing:
            for k, v in values.items():
                setattr(existing, k, v)
        else:
            session.add(ChictrMeta(uuid=doc_uuid, **values))
