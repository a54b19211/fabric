"""Generates tests/fixtures/measure_golden.json with BioRadar's own measure rules (measure.v10), so that
clean.measure_rules can be compared with them field by field. Not part of the test suite: it imports BioRadar.

    # 1. the cleaned texts, from this checkout's WHO DON cleaner
    .venv/bin/python tests/fixtures/measure_golden_make.py texts > /tmp/texts.json
    # 2. BioRadar's windows, contexts, quote placement and request bodies for every (window, measure) pair
    PYTHONPATH=<bioradar checkout>/services <bioradar checkout>/.venv/bin/python \
        tests/fixtures/measure_golden_make.py golden /tmp/texts.json > tests/fixtures/measure_golden.json

Step 2 calls BioRadar's windows_of (continuation), _contexts (continuation, through material.Material), final_of /
build (measure_checks) and schemas.Measure; the quote placement and the cited-source lookup are the few lines of
measure_checks.enqueue / cited_sources that read BioRadar's tables, restated here over the same window and contexts.
"""

import json
import sys
from types import SimpleNamespace

ROWS = "\n".join(f"| District {n:02d} | {n * 3} | {n} |" for n in range(1, 75))
OVERVIEW = (
    "<h2>Situation at a glance</h2>"
    "<p>As of 28 October 2026, a total of 66 confirmed cases of Marburg virus disease, including 15 deaths, have "
    "been reported in Rwanda. The case fatality ratio among confirmed cases is 23% (15/66).</p>"
    "<p>Since the last update, 1 444 contacts have been listed and 1 210 of them have completed follow-up.</p>"
    + "".join(f"<p>Paragraph {n}: response teams continue contact tracing, infection prevention and community "
              f"engagement in the affected districts; no new information on case numbers is given here.</p>"
              for n in range(1, 9))
)
EPIDEMIOLOGY = (
    "<h3>Cases by district</h3><table><caption>Table 1. Confirmed cases and deaths by district</caption>"
    "<thead><tr><th>District</th><th>Confirmed cases</th><th>Deaths</th></tr></thead><tbody>"
    + "".join(f"<tr><td>District {n:02d}</td><td>{n * 3}</td><td>{n}</td></tr>" for n in range(1, 75))
    + "</tbody></table><p>In District 40, 120 confirmed cases were reported between 1 October and 28 October "
    "2026.</p>"
)
RAW = {"Id": "8c6e4a3e-2b0e-4b8e-9d55-1d1b1c7f5a01", "Title": "Marburg virus disease - Rwanda",
       "Overview": OVERVIEW, "Epidemiology": EPIDEMIOLOGY,
       "Summary": "<p>WHO reports 66 confirmed cases of Marburg virus disease in Rwanda.</p>"}
SHORT = {"Id": "8c6e4a3e-2b0e-4b8e-9d55-1d1b1c7f5a02", "Title": "Mpox - Democratic Republic of the Congo",
         "Summary": "<p>In 2026, 1 444 suspected cases of mpox were reported in Équateur.</p>"}

MEASURES = [
    {"object_text": "Marburg virus disease", "metric_text": "confirmed cases", "value_text": "66",
     "unit_text": "cases", "period_text": "28 October 2026", "period": "2026-10-28", "period_type": "point",
     "scope_text": "Rwanda", "kind": "cumulative", "family": "case_count",
     "quote": "As of 28 October 2026, a total of 66 confirmed cases of Marburg virus disease",
     "dims": [{"kind": "disease", "text": "Marburg virus disease"}, {"kind": "population", "text": "Rwanda"}],
     "sources": {"scope": "window"}},
    {"object_text": "Marburg virus disease", "metric_text": "deaths", "value_text": "15", "kind": "cumulative",
     "quote": "including 15 deaths", "period_text": "28 October 2026", "period": "2026-10-28",
     "period_type": "point", "sources": {"period": "window"}},
    {"metric_text": "case fatality ratio", "value_text": "23", "unit_text": "%", "denominator_text": "66",
     "kind": "rate", "quote": "The case fatality ratio among confirmed cases is 23% (15/66)",
     "dims": [{"kind": "denominator_definition", "text": "confirmed cases"}]},
    {"metric_text": "contacts", "value_text": "1 444", "kind": "cumulative",
     "quote": "Since the last update, 1 444 contacts have been listed"},
    {"object_text": "District 40", "metric_text": "confirmed cases", "value_text": "120", "unit_text": "cases",
     "period_text": "between 1 October and 28 October 2026", "period": "2026-10-01/2026-10-28",
     "period_type": "interval", "kind": "flow", "quote": "In District 40, 120 confirmed cases were reported",
     "family": "case_count", "dims": [{"kind": "disease", "text": "Marburg virus disease", "source": "heading"}]},
    {"object_text": "District 37", "metric_text": "Confirmed cases", "value_text": "111", "kind": "cumulative",
     "quote": "| District 37 | 111 | 37 |", "sources": {"object": "quote"},
     "dims": [{"kind": "disease", "text": "Confirmed cases", "source": "table_header"}]},
    {"object_text": "District 52", "metric_text": "Deaths", "value_text": "52", "kind": "cumulative",
     "quote": "| District 52 | 156 | 52 |", "sources": {"scope": "table_title"}, "scope_text": "by district"},
    {"metric_text": "suspected cases of mpox", "value_text": "1 444", "unit_text": "cases", "period_text": "2026",
     "period": "2026", "period_type": "interval", "scope_text": "Équateur", "kind": "flow",
     "quote": "In 2026, 1 444 suspected cases of mpox were reported in Équateur", "family": "case_count"},
    {"metric_text": "confirmed cases", "value_text": "66", "kind": "other",
     "quote": "WHO reports 66 confirmed cases of Marburg virus disease in Rwanda"},
    {"metric_text": "confirmed cases", "value_text": "about 66", "kind": "cumulative",
     "quote": "WHO reports 66 confirmed cases"},
]


def texts():
    sys.path.insert(0, ".")
    from clean import clean_text

    out = []
    for name, raw in (("who_marburg", RAW), ("who_mpox_short", SHORT)):
        cleaned = clean_text.clean_who(json.dumps(raw, ensure_ascii=False))
        out.append({"name": name, "record_id": raw["Id"], "text": cleaned["text"]})
    json.dump({"texts": out, "measures": MEASURES}, sys.stdout, ensure_ascii=False, indent=1)


def _cited(m, sent):
    wanted = {v for v in (m.get("sources") or {}).values() if v != "quote"}
    out = {}
    for name in sorted(wanted):
        if name not in sent:
            return None, "source_not_sent_to_model"
        out[name] = sent[name]
    found, failed = {}, {}
    for name in sorted({d.get("source") for d in m.get("dims") or []} - {None, "quote"}):
        if name in sent:
            found[name] = sent[name]
        else:
            failed[name] = "source_not_sent_to_model"
    return ({**found, **out}, failed), None


def _one(text, a, b, m, sent, base):
    import re

    from bioradar.research import measure_checks as mc

    hits = [x.start() for x in re.finditer(re.escape(m["quote"]), text)] if m["quote"] else []
    anywhere = bool(hits)
    hits = [h for h in hits if a <= h and h + len(m["quote"]) <= b]
    if len(hits) != 1:
        return {"reason": "quote_in_several_places" if hits else "quote_not_in_window" if anywhere
                else "quote_not_in_text"}
    got, why = _cited(m, sent)
    if got is None:
        return {"quote": [hits[0], hits[0] + len(m["quote"])], "reason": why}
    cited, failed = got
    candidate = SimpleNamespace(measure=m, quote_start=hits[0], quote_end=hits[0] + len(m["quote"]), id=1, **base)
    built, why = mc.build(candidate, text, cited, failed)
    if built is None:
        return {"quote": [candidate.quote_start, candidate.quote_end], "reason": why}
    body, final = built
    return {"quote": [candidate.quote_start, candidate.quote_end], "body": body, "final": final}


def golden(path):
    from bioradar.material import Material
    from bioradar.research import continuation, schemas

    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    measures = [schemas.Measure.model_validate(m).model_dump() for m in data["measures"]]
    cases = []
    for t in data["texts"]:
        import hashlib

        text = t["text"]
        sha = hashlib.sha256(text.encode()).hexdigest()
        part = {"kind": "clean_text", "processor_version": "datafabric.clean_text.v12", "version_id": "a" * 64,
                "raw_sha256": "b" * 64}
        material = Material.part(text, part)
        base = {"source": "who_don", "record_id": t["record_id"], "version_id": "a" * 64, "raw_sha256": "b" * 64,
                "is_demo": False, "processor_version": "datafabric.clean_text.v12", "text_sha256": sha}
        windows = continuation.windows_of(text, 300)
        out = []
        for k, (a, b) in enumerate(windows):
            ctx = continuation._contexts(material, a, b)
            contexts = {name: ([[s["start"], s["end"]] for s in c.binding()["segments"] if s.get("part")] if c else None)
                        for name, (_at, c) in ctx.items()}
            sent = {"window": [(a, b)], **{n: [tuple(r) for r in v] for n, v in contexts.items() if v}}
            results = [_one(text, a, b, m, sent, base) for m in measures]
            out.append({"window": [a, b], "contexts": contexts, "results": results})
        cases.append({"name": t["name"], "record_id": t["record_id"], "text": text, "text_sha256": sha,
                      "windows": out})
    json.dump({"generated_by": "bioradar b452279 measure.v10 (measure_checks.build, continuation.windows_of/_contexts)",
               "overlap": 300, "inputs": data["measures"], "measures": measures, "cases": cases},
              sys.stdout, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    texts() if sys.argv[1] == "texts" else golden(sys.argv[2])
