"""processing.measure_extract: the output it accepts, the producer identity, the reading identity, one window."""

import json

from processing import measure_extract as mx
from processing import measure_rules as rules

TEXT = ("# Marburg virus disease - Rwanda\n\nAs of 28 October 2026, a total of 66 confirmed cases of Marburg virus "
        "disease have been reported in Rwanda.\n")
QUOTE = "As of 28 October 2026, a total of 66 confirmed cases of Marburg virus disease"
CONFIG = mx.ModelConfig(kind="openai_compatible", model="fake-model", endpoint_id="loopback", thinking=False,
                        temperature=0.0, max_tokens=2000)


def measure(**change):
    m = {"object_text": "Marburg virus disease", "metric_text": "confirmed cases", "value_text": "66",
         "unit_text": "cases", "period_text": "As of 28 October 2026", "period": "2026-10-28", "period_type": "point",
         "kind": "cumulative", "family": "case_count", "quote": QUOTE,
         "dims": [{"kind": "disease", "text": "Marburg virus disease"}]}
    m.update(change)
    return mx.Measure.model_validate(m).model_dump()


def keys(config=CONFIG, text_sha="c" * 64):
    sha = config.sha256()
    return {"material": {"source_id": "who_don", "record_id": "r1", "version_id": "a" * 64, "raw_sha256": "b" * 64},
            "artifact": {"text_processor_version": "datafabric.clean_text.v12", "text_sha256": text_sha},
            "producer": {"name": mx.PRODUCER, "version": mx.PROCESSOR_VERSION, "model_config_sha256": sha,
                         "identity": mx.producer_identity(sha)}}


SUBJECT = {"source_id": "who_don", "record_id": "r1", "version_id": "a" * 64, "raw_sha256": "b" * 64,
           "is_demo": False, "artifact": {"processor_version": "datafabric.clean_text.v12", "text_sha256": "c" * 64}}


def test_the_output_is_parsed_strictly_and_aliases_are_mapped_mechanically():
    good = {"items": [{"index": 1, "measures": [dict(measure(), sources={"period_text": "window"})]}]}
    measures, mappings, why = mx.parse_output(json.dumps(good))
    assert why is None and measures[0]["sources"] == {"period": "window"}
    assert mappings == [{"item": 0, "measure": 0, "from": "period_text", "to": "period"}]
    assert mx.parse_output("not json")[2] == "output_not_json"
    assert mx.parse_output(json.dumps({"items": [{"index": 1, "measures": [{"quote": "x"}]}]}))[2] == \
        "output_not_the_schema"
    assert mx.parse_output(json.dumps({"items": [{"index": 2, "measures": []}]}))[2] == "no_item"
    many = {"items": [{"index": 1, "measures": [measure()] * 5}]}
    assert len(mx.parse_output(json.dumps(many))[0]) == mx.MAX_MEASURES


# One measure of a real answer of the first D1 measurement (Qwen, json_object and no schema in the prompt; run
# e853c35f, window 0), cut to what decided the refusal: measures at the top level, an index inside the measure,
# empty values for enum fields and sources as a list.
FIRST_ANSWER = {"measures": [{"index": 1, "quote": "4 additional cases", "value_text": "4",
                              "metric_text": "cases of Zika virus infection", "period_text": "On 12 November 2015",
                              "kind": "flow", "qualifier": "", "modality": "", "status": "",
                              "sources": [{"key": "period", "value": "text"}]}]}


def test_the_prompt_carries_the_schema_and_a_skeleton_the_parser_accepts():
    assert mx.SCHEMA in mx.PROMPT and json.loads(mx.SCHEMA) == mx.Extraction.model_json_schema()
    measures, _, why = mx.parse_output(mx.SKELETON)
    assert why is None and len(measures) == 1
    assert mx.parse_output(mx.EMPTY) == ([], [], None)


def test_an_answer_outside_the_schema_is_refused_and_not_repaired():
    assert mx.parse_output(json.dumps(FIRST_ANSWER)) == (None, [], "output_not_the_schema")
    rewrapped = {"items": [{"index": 1, "measures": FIRST_ANSWER["measures"]}]}  # still empty enums, sources a list
    assert mx.parse_output(json.dumps(rewrapped))[2] == "output_not_the_schema"
    conforming = {k: v for k, v in FIRST_ANSWER["measures"][0].items() if k not in ("qualifier", "modality", "status")}
    conforming["sources"] = {"period": "window"}
    measures, _, why = mx.parse_output(json.dumps({"items": [{"index": 1, "measures": [conforming]}]}))
    assert why is None and measures[0]["qualifier"] == "exact" and measures[0]["sources"] == {"period": "window"}


def test_another_model_configuration_is_another_producer():
    other = mx.ModelConfig(**{**CONFIG.__dict__, "model": "fake-model-2"})
    assert CONFIG.sha256() != other.sha256()
    assert mx.ModelConfig(**CONFIG.__dict__).sha256() == CONFIG.sha256()  # stable
    assert "base_url" not in CONFIG.__dict__ and "api_key" not in CONFIG.__dict__  # where and the key are not identity
    identity = mx.producer_identity(CONFIG.sha256())
    assert identity.startswith("datafabric.measure_extract.v3+") and len(identity) <= 80  # the contract's length
    assert mx.run_id(**{k: v for k, v in keys().items()}) != mx.run_id(**keys(other))


def test_the_reading_id_covers_the_whole_candidate():
    sent = rules.Sent((0, len(TEXT)))
    one, _ = mx.readings_of([measure()], TEXT, sent, SUBJECT, keys())
    again, _ = mx.readings_of([measure()], TEXT, sent, SUBJECT, keys())
    assert one[0].reading_id == again[0].reading_id  # deterministic
    # the same span read with another unit (or none) is another reading, checked on its own
    other, _ = mx.readings_of([measure(unit_text="")], TEXT, sent, SUBJECT, keys())
    assert other[0].reading_id != one[0].reading_id
    assert other[0].quote_start == one[0].quote_start
    # the same candidate on another artifact or by another producer is another reading
    assert mx.readings_of([measure()], TEXT, sent, SUBJECT, keys(text_sha="d" * 64))[0][0].reading_id != \
        one[0].reading_id
    # the caller's item id is not part of it
    body = json.loads(json.dumps(one[0].body))
    body["items"][0]["id"] = "m9"
    k = keys()
    assert mx.reading_id(k["material"], k["artifact"], k["producer"], body) == one[0].reading_id


def test_a_window_builds_only_what_it_read_and_says_why_not():
    sent = rules.Sent((0, 40))  # the quote is not inside this window
    drafts, not_built = mx.readings_of([measure(), measure(value_text="sixty-six")], TEXT, sent, SUBJECT, keys())
    assert drafts == [] and not_built == [{"measure": 0, "reason": "quote_not_in_window"},
                                          {"measure": 1, "reason": "quote_not_in_window"}]
    drafts, not_built = mx.readings_of([measure(sources={"period": "heading"})], TEXT, rules.Sent((0, len(TEXT))),
                                       SUBJECT, keys())
    assert not_built == [{"measure": 0, "reason": "source_not_sent_to_model"}]  # no heading was sent


def test_the_request_names_this_producer_and_no_workspace_context():
    drafts, _ = mx.readings_of([measure()], TEXT, rules.Sent((0, len(TEXT))), SUBJECT, keys())
    item = drafts[0].body["items"][0]
    assert item["producer"] == {"name": "datafabric.measure_extract", "version": mx.producer_identity(CONFIG.sha256())}
    assert drafts[0].body["contract_version"] == "extraction_consistency.v3"
    payload = mx.window_payload("Marburg - Rwanda", TEXT, (0, len(TEXT)), {"heading": (0, 32), "table_header": None})
    window = payload["windows"][0]
    assert window["heading"] == {"at": 0, "text": "# Marburg virus disease - Rwanda"} and window["table_header"] is None
    assert window["text"] == TEXT and set(window) == {"index", "title", "position", "heading", "table_header", "text"}
    for word in ("known_series", "topic", "workspace", "专题"):
        assert word not in mx.PROMPT and word not in json.dumps(payload)
