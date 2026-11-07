"""process_entity_merge: how each incoming field meets the canonical value, with a recording fake session."""

from types import SimpleNamespace

import pytest

from db.models import EntityDict, EntityMergeQueue, ExtractedEntity
from db.repository import canonical
from db.repository.canonical import process_entity_merge


class FakeQuery:
    def __init__(self, session, model):
        self.session, self.model = session, model

    def filter(self, *_args):
        return self

    def order_by(self, *_args):
        return self

    def first(self):
        return self.session.rows.get(self.model)


class FakeSession:
    def __init__(self, **rows):
        self.rows = {EntityDict: rows.get("entity"), ExtractedEntity: rows.get("existing_source"),
                     EntityMergeQueue: rows.get("pending")}
        self.added = []

    def query(self, model):
        return FakeQuery(self, model)

    def add(self, row):
        self.added.append(row)


@pytest.fixture
def log(monkeypatch):
    calls = []
    monkeypatch.setattr(canonical, "_write_operation_log", lambda *args, **kwargs: calls.append((args, kwargs)))

    def set_data(_session, entity, *, canonical_data):
        entity.canonical_data = canonical_data
    monkeypatch.setattr("db.identity.set_dictionary_identity_data", set_data)
    return calls


def dictionary(data, source="pubmed", entity_type="NoSuchType"):
    return SimpleNamespace(id=7, canonical_data=data, canonical_source=source, entity_type=entity_type,
                           canonical_updated_at=None)


def merge(data, incoming, site="pubmed", **rows):
    entity = dictionary(data, **{k: rows.pop(k) for k in ("source", "entity_type") if k in rows})
    session = FakeSession(entity=entity, **rows)
    counts = process_entity_merge(session, 7, 99, incoming, site)
    return counts, entity, session


def test_missing_dictionary_is_a_noop(log):
    session = FakeSession(entity=None)
    assert process_entity_merge(session, 7, 99, {"a": 1}, "pubmed") == (0, 0)
    assert not log


@pytest.mark.parametrize("existing, incoming, site, source, counts, result", [
    ({}, {"phone": "1"}, "pubmed", "pubmed", (1, 0), {"phone": "1"}),                       # new field
    ({"phone": "1"}, {"phone": "2"}, "fda", "pubmed", (1, 0), {"phone": "2"}),               # higher priority wins
    ({"phone": "1"}, {"phone": "2"}, "fiercepharma", "pubmed", (0, 0), {"phone": "1"}),      # lower priority ignored
    ({"phone": "1"}, {"phone": "1"}, "pubmed", "pubmed", (0, 0), {"phone": "1"}),            # same value
    ({"phone": ""}, {"phone": "2"}, "fiercepharma", "pubmed", (1, 0), {"phone": "2"}),       # empty existing
    ({"phone": "1"}, {"phone": ""}, "fda", "pubmed", (0, 0), {"phone": "1"}),                # empty incoming
    ({"phone": ""}, {"phone": []}, "fda", "pubmed", (0, 0), {"phone": ""}),                  # both empty
    ({"phone": "1"}, {"phone": None, "_x": 1, "name": "N", "aliases": ["a"]}, "fda", "pubmed", (0, 0), {"phone": "1"}),
    ({"categories": ["a"]}, {"categories": ["b", "a"]}, "fiercepharma", "pubmed", (1, 0), {"categories": ["a", "b"]}),
    ({"categories": ["a", "b"]}, {"categories": ["b"]}, "pubmed", "pubmed", (0, 0), {"categories": ["a", "b"]}),
    ({"categories": ["a"]}, {"categories": "b"}, "fda", "pubmed", (0, 0), {"categories": ["a"]}),
    ({"price": {"timestamp": 1, "v": 1}}, {"price": {"timestamp": 2, "v": 2}}, "fiercepharma", "pubmed", (1, 0),
     {"price": {"timestamp": 2, "v": 2}}),
    ({"price": {"timestamp": 2, "v": 2}}, {"price": {"timestamp": 1, "v": 1}}, "fda", "pubmed", (0, 0),
     {"price": {"timestamp": 2, "v": 2}}),
    ({"price": {"timestamp": 2}}, {"price": "x"}, "fda", "pubmed", (0, 0), {"price": {"timestamp": 2}}),
])
def test_field_decisions_that_change_or_keep_the_canonical_data(existing, incoming, site, source, counts, result, log):
    (auto, conflicts), entity, session = merge(dict(existing), incoming, site, source=source)
    assert (auto, conflicts) == counts and not session.added
    assert {k: v for k, v in entity.canonical_data.items() if k in result} == result
    assert bool(log) == bool(auto)
    assert (entity.canonical_updated_at is not None) == bool(auto)


def test_same_priority_conflict_is_queued_with_both_sources(log):
    older = SimpleNamespace(id=5, source_doc_uuid="old-doc")
    (auto, conflicts), entity, session = merge({"phone": "1", "tags": {"a": 1}}, {"phone": "2", "tags": [1]},
                                               existing_source=older)
    assert (auto, conflicts) == (0, 2) and entity.canonical_data == {"phone": "1", "tags": {"a": 1}}
    rows = {row.field_name: row for row in session.added}
    assert rows["phone"].existing_value == {"_val": "1"} and rows["phone"].new_value == {"_val": "2"}
    assert rows["tags"].existing_value == {"a": 1} and rows["tags"].new_value == [1]
    assert all(row.source_entity_id == 99 and row.target_dict_id == 7 and row.source_site == "pubmed"
               and row.existing_source_entity_id == 5 and row.existing_source_doc_uuid == "old-doc"
               and row.resolution == "pending" for row in rows.values())
    assert log and log[0][1]["details"]["conflicts"] == 2


def test_same_priority_conflict_refreshes_the_open_row_instead_of_queueing_another(log):
    pending = SimpleNamespace(existing_value=None, new_value=None, source_entity_id=1, source_site="x",
                              existing_source_entity_id=None, existing_source_doc_uuid="")
    (auto, conflicts), _entity, session = merge({"phone": "1"}, {"phone": "2"}, pending=pending)
    assert (auto, conflicts) == (0, 0) and not session.added and not log
    assert pending.existing_value == {"_val": "1"} and pending.new_value == {"_val": "2"}
    assert pending.source_entity_id == 99 and pending.source_site == "pubmed"
    assert pending.existing_source_entity_id is None and pending.existing_source_doc_uuid == ""


def test_fields_outside_the_entity_type_whitelist_are_ignored(log):
    whitelist = canonical._VALID_FIELDS_BY_TYPE
    entity_type = next(iter(whitelist))
    allowed = next(f for f in sorted(whitelist[entity_type])
                   if not f.startswith("_") and f not in canonical._SKIP_MERGE_FIELDS
                   and f not in canonical._ARRAY_FIELDS and f not in canonical._SNAPSHOT_FIELDS)
    (auto, _), entity, _session = merge({}, {allowed: "v", "zz_not_a_field": "v"}, entity_type=entity_type)
    assert auto == 1 and entity.canonical_data == {allowed: "v"}
