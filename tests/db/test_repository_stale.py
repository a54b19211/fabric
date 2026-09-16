"""db.repository 字典更新级联重算（状态陈旧修复）的单元测试。"""
from db.models import EntityDict, EntityAlias, ExtractedEntity
from db.repository import (
    _is_stale_step_none,
    _refresh_step_metadata,
    _cascade_recompute_stale_entities,
)


def _entity(name="Pacsta", etype="Company", dict_id=100,
            reason="no match in any step", disambig=None, status="pending"):
    ed = {}
    if disambig is not None:
        ed["_disambig"] = disambig
    return ExtractedEntity(
        entity_name=name,
        entity_type=etype,
        dict_id=dict_id,
        confidence_reason=reason,
        confidence_score=0.0,
        review_status=status,
        entity_data=ed,
    )


class _FakeQuery:
    def __init__(self, first=None, all_=None):
        self._first = first
        self._all = all_ if all_ is not None else []

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._first

    def all(self):
        return self._all


class _FakeSession:
    def __init__(self, target=None, alias_names=None, candidates=None):
        self.target = target
        self.alias_names = alias_names or []
        self.candidates = candidates or []
        self.added = []

    def query(self, *entities):
        head = entities[0]
        if isinstance(head, type) and head is EntityDict:
            return _FakeQuery(first=self.target)
        if isinstance(head, type) and head is ExtractedEntity:
            return _FakeQuery(all_=self.candidates)
        # EntityAlias.name 等 InstrumentedAttribute → alias 行
        return _FakeQuery(all_=[(n,) for n in self.alias_names])

    def add(self, obj):
        self.added.append(obj)


class TestIsStaleStepNone:
    def test_by_reason(self):
        assert _is_stale_step_none(_entity(reason="no match in any step")) is True

    def test_by_disambig(self):
        ent = _entity(reason="", disambig={"match_step": "step_none", "status": "new_entity"})
        assert _is_stale_step_none(ent) is True

    def test_normal_entity_false(self):
        ent = _entity(reason="alias exact match: 'x'", disambig={"match_step": "step2_exact_alias"})
        assert _is_stale_step_none(ent) is False

    def test_empty_metadata_false(self):
        ent = _entity(reason="", disambig={})
        assert _is_stale_step_none(ent) is False


class TestRefreshStepMetadata:
    def test_refresh_fields(self):
        ent = _entity(dict_id=100, disambig={"match_step": "step_none", "status": "new_entity"})
        _refresh_step_metadata(ent, 200)
        assert ent.confidence_reason == "stale_recompute: alias exact match"
        assert ent.confidence_score == 0.95
        disambig = ent.entity_data["_disambig"]
        assert disambig["match_step"] == "step2_exact_alias"
        assert disambig["status"] == "auto_matched"
        assert disambig["dict_id"] == 200

    def test_preserves_other_entity_data(self):
        ent = _entity(disambig={"match_step": "step_none"})
        ent.entity_data["extra"] = "keep"
        _refresh_step_metadata(ent, 200)
        assert ent.entity_data["extra"] == "keep"


class TestCascadeRecompute:
    def test_no_target_returns_zero(self):
        session = _FakeSession(target=None)
        assert _cascade_recompute_stale_entities(session, 999) == 0

    def test_no_aliases_returns_zero(self):
        target = EntityDict(id=200, entity_type="Company")
        session = _FakeSession(target=target, alias_names=[], candidates=[])
        assert _cascade_recompute_stale_entities(session, 200) == 0

    def test_reassigns_stale_entity(self):
        target = EntityDict(id=200, entity_type="Company")
        stale = _entity(name="Pacsta", dict_id=100,
                        disambig={"match_step": "step_none", "status": "new_entity"})
        session = _FakeSession(target=target, alias_names=["Pacsta", "pacsta"],
                               candidates=[stale])
        assert _cascade_recompute_stale_entities(session, 200) == 1
        assert stale.dict_id == 200
        assert stale.entity_data["_disambig"]["match_step"] == "step2_exact_alias"
        assert stale.entity_data["_disambig"]["status"] == "auto_matched"
        assert len(session.added) == 1  # 写了一条 operation_log

    def test_skips_non_stale_entity(self):
        target = EntityDict(id=200, entity_type="Company")
        matched = _entity(name="Pacsta", dict_id=100,
                          reason="alias exact match",
                          disambig={"match_step": "step2_exact_alias"})
        session = _FakeSession(target=target, alias_names=["Pacsta"],
                               candidates=[matched])
        assert _cascade_recompute_stale_entities(session, 200) == 0
        assert matched.dict_id == 100  # 未被改动

    def test_already_on_target_also_refreshed(self):
        # dict_id 已指向目标 dict，但 match_step 陈旧 → 仍刷新元数据
        target = EntityDict(id=200, entity_type="Company")
        stale = _entity(name="Pacsta", dict_id=200,
                        disambig={"match_step": "step_none"})
        session = _FakeSession(target=target, alias_names=["Pacsta"],
                               candidates=[stale])
        assert _cascade_recompute_stale_entities(session, 200) == 1
        assert stale.dict_id == 200
        assert stale.entity_data["_disambig"]["match_step"] == "step2_exact_alias"
