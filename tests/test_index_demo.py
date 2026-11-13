"""索引演示标记随固定原件传递，不由KB名称或字符串scope猜测。"""
from types import SimpleNamespace

import pytest

from store import index_publication as index
from store.history_models import HistoryError


def values(demo):
    record = SimpleNamespace(source_id="external_archive", source_record_id="one", version_id="a"*64,
                             raw_sha256="b"*64, title="synthetic", clean_text="synthetic", is_demo=demo, facts={})
    target = {"source_instance":"synthetic", "source_id":"external_archive", "is_demo":demo,
              "knowledge_base_id":"synthetic-kb", "source_scope":"simulation" if demo else "research"}
    return target, record


ARTIFACT = {"processor_version": "clean_text.synthetic", "text_sha256": "d" * 64, "locator_kind": None}


def manifest(target, record):
    return index._manifest(target, record, ARTIFACT, {})


@pytest.mark.parametrize("demo", [False, True])
def test_manifest_keeps_explicit_demo(demo):
    target, record = values(demo)
    assert manifest(target, record)["is_demo"] is demo


def test_manifest_refuses_target_space_mismatch():
    target, record = values(True)
    target["is_demo"] = False
    with pytest.raises(HistoryError):
        manifest(target, record)


def test_receipt_cannot_drop_or_flip_demo_marker():
    target, record = values(True)
    built = manifest(target, record)
    row = {"manifest":built, "config":target, "remote_id":"c"*64}
    receipt = {"publication_id":row["remote_id"], "knowledge_base_id":target["knowledge_base_id"],
               "source_scope":target["source_scope"], "content_sha256":built["content_sha256"],
               "identity":{k:built[k] for k in ("source_instance","source_id","source_record_id","version_id","raw_sha256",
                                                 "artifact_key")},
               "raw_locator":built["raw_locator"]}
    for marker in [None, False]:
        if marker is not None:
            receipt["is_demo"] = marker
        with pytest.raises(HistoryError):
            index._validate_receipt(row, receipt)
    receipt["is_demo"] = True
    index._validate_receipt(row, receipt)
