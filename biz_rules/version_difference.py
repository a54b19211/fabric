"""What differs between two adjacent versions of one source record: plain facts, no judgment.

Version identity includes the parser version and the whole projection (db.history._version_identity), so a new
version can come from three different causes. This module names which one it was, from the two stored versions
alone:

- raw_changed: the original itself changed. The flags say whether the source's own update time, the body text
  or any other projected field moved with it. Nothing here says whether the change matters to a conclusion.
- projection_only: the original is the same, the parser or the projection differs (a parser upgrade).
- serialization_only: the original means the same (key order, spacing, a field the source rewrites on every
  response, see history_sources.stable_raw) and the projection is identical; also the case of the same original
  and projection under another availability proof.

Two kinds of "reprocessing" are different things, and neither is a correction by the source. A parser or
projection upgrade can give an unchanged original a new version_id: it shows here as projection_only. Re-running
the cleaning processor on the same original only adds a derived artifact with its own artifact_key (processor
version : body hash, db.index_publication.artifact_key) and not necessarily a History version: it never shows here.
"""

from __future__ import annotations

from biz_rules.history_sources import stable_raw

# The fields a source projection fills in (history_sources._assemble) next to the original. Together they are the
# projection that version identity covers.
STRUCTURE_FIELDS = (
    "record_kind", "title", "source_url", "facts", "field_locators", "event_time", "source_published_at",
)
PROJECTED_FIELDS = STRUCTURE_FIELDS + ("clean_text", "source_updated_at", "parser_version")


def _same_original(source_id: str, old: dict, new: dict) -> bool:
    if old.get("raw_sha256") == new.get("raw_sha256"):
        return True
    stable = stable_raw(source_id, old.get("raw_content"))
    return stable is not None and stable == stable_raw(source_id, new.get("raw_content"))


def _update_stamp(payload: dict) -> tuple:
    """The source's own update time without its meaning label: a relabelled time is not a changed time."""
    stamp = payload.get("source_updated_at") or {}
    return stamp.get("value"), stamp.get("precision")


def classify(source_id: str, old: dict, new: dict) -> dict:
    """The kind of difference from `old` to `new` (stored payloads of two versions of one record) and, for every
    kind, what changed in the projection: the parser version, the body text (`clean_text`), the source's own
    update time, any other projected field (`structure_changed`: facts, locators, title and the rest). The flags say
    where to look, not how much changed; a source whose facts repeat its body text or update time (WHO) also sets
    `structure_changed` when those change."""
    flags = {
        "parser_changed": old.get("parser_version") != new.get("parser_version"),
        "body_changed": old.get("clean_text") != new.get("clean_text"),
        "last_modified_changed": _update_stamp(old) != _update_stamp(new),
        "structure_changed": any(old.get(field) != new.get(field) for field in STRUCTURE_FIELDS),
    }
    if not _same_original(source_id, old, new):
        kind = "raw_changed"
    elif any(old.get(field) != new.get(field) for field in PROJECTED_FIELDS):
        kind = "projection_only"
    else:
        kind = "serialization_only"
    return {"kind": kind, **flags}
