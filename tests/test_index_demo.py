"""发给桥接的请求体：演示标记随索引目标走；改动前入队的清单里只留在本地的字段不发。"""
from types import SimpleNamespace

import pytest

from store import index_publication as index

ARTIFACT = {"processor_version": "clean_text.synthetic", "text_sha256": "d" * 64}


def target(demo):
    return index.IndexTarget(source_instance="synthetic", source_id="external_archive", is_demo=demo,
                             knowledge_base_id="synthetic-kb", source_scope="simulation" if demo else "research")


def record():
    return SimpleNamespace(source_id="external_archive", source_record_id="one", version_id="a" * 64,
                           raw_sha256="b" * 64, title="synthetic")


@pytest.mark.parametrize("demo", [False, True])
def test_the_body_carries_the_targets_demo_flag_and_only_bridge_fields(demo):
    manifest = index._manifest(target(demo), record(), ARTIFACT, {"source_id": "external_archive"})
    assert manifest["is_demo"] is demo and set(manifest) <= set(index.BRIDGE_FIELDS)
    older = {**manifest, "processor_version": "x", "artifact_sha256": "d" * 64, "locator_kind": None,
             "parent_record_id": None}
    assert index._body({"manifest": older}, "text") == {**manifest, "content": "text"}
