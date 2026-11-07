"""The measure rules of the extraction processor datafabric.measure_extract.v2: how a cleaned artifact is cut into
reading windows, what context goes along with a window, and how one measure a model wrote for a window becomes a
strict BioCheck contract-v3 request item - every literal located at its real offset in the artifact, or not built.

These rules were moved from BioRadar (measure.v10: research/measure_checks.py, research/continuation.py and
material.py at b452279) without changing them; tests/test_measure_rules.py compares them with BioRadar's output on
the same artifacts and the same model outputs (tests/fixtures/measure_golden.json, docs/decisions/0006). Only the
places that read BioRadar's own tables changed: a measure's cited sources are the window and the contexts this
processor itself sent with it (`Sent`), never a workspace's records.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import extraction_contract as ec
from extraction_contract import normalize as norm

WINDOW = 1500  # characters of one reading window
OVERLAP = 300  # each window after the first repeats this much of the one before
CONTEXT_WINDOW, MAX_CHARS = 200, 20000
STOP = "。；;！？!?\n"
SENTENCE_END = "。！？!?；;"
MODIFIERS = r"(?:(?:suspected|confirmed|probable|laboratory-confirmed|new|human|associated)\s+){0,2}"
# a measure may cite the source of its period / object / scope / denominator literal - the quote, the window,
# or a context sent with the window; it is looked up there only
SOURCE_FIELDS = ("period", "object", "scope", "denominator")
CONTEXT_SOURCES = ("heading", "table_header", "table_title")
CAPTION = re.compile(r"\s*(?:Table|TABLE)\s+\d+|\s*表\s*\d+")
HEADING = re.compile(r"(?m)^#{1,6} [^\n]*")
_TABLE_RULE = re.compile(r"^\|?\s*:?-{3,}")
HEADING_CHARS, TABLE_HEADER_CHARS, TABLE_TITLE_CHARS = 200, 600, 300


# ---- windows ---------------------------------------------------------------------------------------------------


def _is_cut(text, p, line_only):
    """Whether position p (between text[p-1] and text[p]) is a boundary."""
    c = text[p - 1]
    if c == "\n":
        return True
    if line_only:
        return False
    return c in SENTENCE_END or (c == "." and (p == len(text) or text[p] in " \n"))


def _cut_end(text, lo, hi):
    """The last boundary in (lo, hi]: a line end first, else a sentence end; hi when there is none."""
    for line_only in (True, False):
        for p in range(hi, lo, -1):
            if _is_cut(text, p, line_only):
                return p
    return hi


def _cut_start(text, lo, hi):
    """The first boundary in [lo, hi): a line start first, else a sentence start; lo when there is none."""
    for line_only in (True, False):
        for p in range(max(lo, 1), hi):
            if _is_cut(text, p, line_only):
                return p
    return lo


def windows_of(text, overlap=OVERLAP, window=WINDOW):
    """[(start, end)]: window 0 is [0, window); each later one starts at a boundary inside the last `overlap`
    characters of the one before and ends at the last boundary of its `window` characters (keeping at least two
    thirds of them)."""
    n = len(text)
    out = [(0, min(window, n))]
    while out[-1][1] < n:
        a_prev, b_prev = out[-1]
        a = _cut_start(text, max(b_prev - overlap, a_prev + 1), b_prev)
        b = min(a + window, n)
        if b < n:
            b = _cut_end(text, a + window * 2 // 3, b)
        out.append((a, b))
    return out


# ---- tables and the context of a window ------------------------------------------------------------------------


def tables_of(text):
    """The markdown tables of the text: each table's header (its first row and the separator row under it, with the
    newline after them) and its data rows [start, end). Rows run while lines start with "|"; a row followed by a
    separator row starts a new table, so two tables written back to back never share a header."""
    lines, pos = [], 0
    for line in text.split("\n"):
        lines.append((pos, pos + len(line), line))
        pos += len(line) + 1

    def heads(i):
        return (i + 1 < len(lines) and lines[i][2].startswith("|") and lines[i + 1][2].startswith("|")
                and bool(_TABLE_RULE.match(lines[i + 1][2].strip())))

    out, i = [], 0
    while i < len(lines):
        if not heads(i):
            i += 1
            continue
        header_start, header_end = lines[i][0], min(lines[i + 1][1] + 1, len(text))
        j = i + 2
        while j < len(lines) and lines[j][2].startswith("|") and not heads(j):
            j += 1
        if j > i + 2:
            out.append({"header_start": header_start, "header_end": header_end, "start": header_end,
                        "end": lines[j - 1][1]})
        i = j
    return out


def _table_title(text, header_start):
    """The "Table N" / "表N" caption line right above a table, as (start, end), or None: only the first non-empty
    line above the table can be its caption."""
    end = text.rfind("\n", 0, header_start)  # the line break before the header row
    while end > 0:
        start = text.rfind("\n", 0, end) + 1
        line = text[start:end]
        if line.strip():
            return (start, min(end, start + TABLE_TITLE_CHARS)) if CAPTION.match(line) else None
        end = start - 1
    return None


def contexts_of(text, a, tables=None):
    """The context of the window starting at a, as artifact ranges (start, end) or None: the nearest markdown
    heading before it, and - when the window starts inside a table's data rows - that table's header and the
    caption line right above the table. Context is sent beside the window text, never as window text."""
    out = {"heading": None, "table_header": None, "table_title": None}
    heading = None
    for m in HEADING.finditer(text[:a]):
        heading = m
    if heading is not None:
        out["heading"] = (heading.start(), heading.start() + len(heading.group(0)[:HEADING_CHARS]))
    for t in tables if tables is not None else (tables_of(text) if "|" in text else []):
        if t["start"] <= a < t["end"]:
            hs, he = t["header_start"], t["header_end"]
            out["table_header"] = (hs, min(he, hs + TABLE_HEADER_CHARS))
            out["table_title"] = _table_title(text, hs)
            break
    return out


@dataclass(frozen=True)
class Sent:
    """What this processor sent the model for one window: the window range and each context it sent (artifact
    ranges). A measure may cite only these."""

    window: tuple[int, int]
    contexts: dict = field(default_factory=dict)

    def ranges(self, name):
        if name == "window":
            return [self.window]
        got = self.contexts.get(name)
        return [got] if got else None


# ---- final values ------------------------------------------------------------------------------------------


def structured_period(period, period_type):
    """The model's normalized period string -> the contract's period, or None (a format reading only: what it
    means is BioCheck's to check against the written period)."""
    p, t = (period or "").strip().upper(), period_type
    if not p or t not in ("point", "interval"):
        return None
    forms = (("day", r"\d{4}-\d{2}-\d{2}"), ("month", r"\d{4}-\d{2}"), ("quarter", r"\d{4}Q[1-4]"),
             ("half", r"\d{4}H[12]"), ("year", r"\d{4}"))
    if "/" in p:
        a, _, b = p.partition("/")
        for precision, form in forms[:2]:
            if re.fullmatch(form, a) and re.fullmatch(form, b) and t == "interval":
                return {"type": "interval", "precision": precision, "start": a, "end": b}
        return None
    for precision, form in forms:
        if re.fullmatch(form, p):
            return {"type": t, "precision": precision, "value": p}
    return None


def final_of(m):
    """The values the check is about and a measurement object would carry - or (None, reason)."""
    value = norm.decimal_text(m["value_text"])
    if value is None:
        return None, "value_not_a_supported_number"
    denominator = None
    if m.get("denominator_text"):
        denominator = norm.decimal_text(m["denominator_text"])
        if denominator is None:
            return None, "denominator_not_a_supported_number"
    period = structured_period(m.get("period"), m.get("period_type"))
    if (m.get("period_text") or m.get("period")) and period is None:
        return None, "period_form_not_supported"
    scope_mode = "stated" if m.get("scope_text") else "object_metric_total" if m.get("object_text") else "unknown"
    return {"object": m.get("object_text") or "", "metric": m["metric_text"], "value": value,
            "unit": m.get("unit_text") or "", "period": period, "scope": m.get("scope_text") or "",
            "scope_mode": scope_mode, "denominator": denominator, "measure_kind": m["kind"],
            "qualifier": m.get("qualifier") or "exact", "modality": m.get("modality") or "actual"}, None


# ---- locating literals -------------------------------------------------------------------------------------


def clause(text, a, b):
    s = max((text.rfind(c, 0, a) for c in STOP), default=-1) + 1
    ends = [i for i in (text.find(c, b) for c in STOP) if i != -1]
    return s, (min(ends) + 1 if ends else len(text))


def place_quote(text, quote, window):
    """(start, end) of the quote's one occurrence inside the window it was read from, or (None, reason): its
    offset there tells a repeated sentence apart, and a quote the model cannot have read is not built."""
    hits = [x.start() for x in re.finditer(re.escape(quote), text)] if quote else []
    anywhere = bool(hits)
    hits = [h for h in hits if window[0] <= h and h + len(quote) <= window[1]]
    if len(hits) == 1:
        return (hits[0], hits[0] + len(quote)), None
    return None, ("quote_in_several_places" if hits else "quote_not_in_window" if anywhere else "quote_not_in_text")


def cited_sources(measure, sent):
    """({source: [ranges]}, None) for every non-quote source the measure's fields cite, or (None, reason): a field
    source must have been sent with this window, else the measure is not built."""
    wanted = {v for v in (measure.get("sources") or {}).values() if v != "quote"}
    out = {}
    for name in sorted(wanted):
        ranges = sent.ranges(name)
        if not ranges:
            return None, "source_not_sent_to_model"
        out[name] = ranges
    return out, None


def cited_dim_sources(measure, sent):
    """({source: ranges}, {source: why}) for the sources the stated dimensions cite, each resolved on its own: one
    not sent with this window only leaves its dimensions unlocated."""
    found, failed = {}, {}
    for name in sorted({d.get("source") for d in measure.get("dims") or []} - {None, "quote"}):
        ranges = sent.ranges(name)
        if ranges:
            found[name] = ranges
        else:
            failed[name] = "source_not_sent_to_model"
    return found, failed


def _of_value_table(text, at, source, ranges, spans):
    """Whether a literal found at `at` in a table header / caption is that of the value's own table."""
    v = spans["value"][0]
    table = next((t for t in tables_of(text) if t["start"] <= v < t["end"]), None)
    if table is None:
        return False
    if source == "table_header":
        return table["header_start"] <= at < table["header_end"]
    end = next(b for a, b in ranges if a <= at < b)
    return not (end > table["header_start"] or text[end:table["header_start"]].strip())


def in_source(text, q0, q1, name, literal, source, cited, spans):
    """(offset, None) of the literal's one occurrence in the cited source, or (None, reason)."""
    ranges = [(q0, q1)] if source == "quote" else (cited or {}).get(source)
    if not ranges:
        return None, "source_not_sent_to_model"
    hits = []
    for a, b in ranges:
        for x in re.finditer(re.escape(literal), text[a:b]):
            at = a + x.start()
            if name == "denominator" and not norm.is_whole_number(text, at, at + len(literal)):
                continue  # a number of the source, whole - "851" of "7 851" is not one
            hits.append(at)
    if not hits:
        return None, f"{name}_not_in_cited_source"
    if len(hits) > 1:
        return None, f"{name}_ambiguous_in_cited_source"
    at = hits[0]
    # the context of the value's own table: never the header or caption of a neighbouring table
    if source in ("table_header", "table_title") and not _of_value_table(text, at, source, ranges, spans):
        return None, "source_not_of_value_table"
    return at, None


def locate_dims(text, q0, q1, m, cited, spans, failed=None):
    """({kind: (start, end)}, [unlocated]) of the dimensions the measure states, each looked up in its cited source
    only; an unlocated one is kept with why - never dropped as if never stated."""
    located, unlocated, seen = {}, [], set()
    for d in m.get("dims") or []:
        kind, literal, source = d.get("kind"), d.get("text") or "", d.get("source") or "quote"
        if kind in seen or kind not in ec.DIM_KINDS or not literal:
            unlocated.append({"kind": kind, "text": literal, "source": source, "reason": "dim_not_sendable"})
            continue
        seen.add(kind)
        if source in (failed or {}):
            unlocated.append({"kind": kind, "text": literal, "source": source, "reason": failed[source]})
            continue
        at, why = in_source(text, q0, q1, f"dim_{kind}", literal, source, cited, spans)
        if why:
            unlocated.append({"kind": kind, "text": literal, "source": source, "reason": why})
            continue
        located[kind] = (at, at + len(literal))
    return located, unlocated


def _find(text, q0, q1, c0, c1, name, literal):
    """The literal's one occurrence in the quote, else in its clause; numbers as whole tokens. None when absent or
    ambiguous."""
    pat = re.escape(literal)
    if name in ("value", "denominator"):
        # a whole number token of its clause: "851" of "7 851" is not one (extraction_normalize.v1)
        whole = set(norm.number_tokens(text[c0:c1]))
        inq = [x.start() for x in re.finditer(pat, text[q0:q1])
               if (q0 + x.start() - c0, q0 + x.end() - c0) in whole]
        if len(inq) == 1:
            return q0 + inq[0]
        if inq:
            return None
        inc = [x.start() for x in re.finditer(pat, text[c0:c1]) if (x.start(), x.end()) in whole]
        return c0 + inc[0] if len(inc) == 1 else None
    inq = [x.start() for x in re.finditer(pat, text[q0:q1])]
    if len(inq) == 1:
        return q0 + inq[0]
    if inq:
        return None
    inc = [x.start() for x in re.finditer(pat, text[c0:c1])]
    return c0 + inc[0] if len(inc) == 1 else None


def _unit_span(text, m, spans):
    """(start, end) of the unit literal, or (None, reason): right after the value (a count noun after at most two
    listed modifiers), else the head counting noun of the bound metric phrase's subject."""
    unit = m.get("unit_text") or ""
    v_end = spans["value"][1]
    hit = re.match(r"\s*" + MODIFIERS, text[v_end:v_end + 80], re.IGNORECASE)
    if text[v_end + hit.end():].startswith(unit):
        u = v_end + hit.end()
        m0, m1 = spans.get("metric", (0, 0))
        if m0 <= u < u + len(unit) <= m1 and not norm.unit_subject_head_v4(text, v_end, u, u + len(unit), m0, m1):
            # right after the value but a word of the metric phrase that is not its head counting noun ("close" of
            # "23 close contacts"): not the unit (extraction_normalize.v3)
            return None, "unit_literal_not_at_the_value"
        return (u, u + len(unit)), None
    if "metric" in spans:  # the head of the bound metric phrase's subject (extraction_normalize.v3)
        m0, m1 = spans["metric"]
        u = text.find(unit, m0, m1)
        if u < 0 or not norm.unit_subject_head_v4(text, v_end, u, u + len(unit), m0, m1):
            return None, "unit_literal_not_at_the_value"
        return (u, u + len(unit)), None
    return None, "unit_literal_not_at_the_value"


LITERALS = (("value", "value_text"), ("denominator", "denominator_text"), ("object", "object_text"),
            ("metric", "metric_text"), ("period", "period_text"), ("scope", "scope_text"))


def locate(text, q0, q1, m, cited=None):
    """Real offsets in the artifact of every literal of the measure: in the quote once, else its clause once;
    numbers as whole tokens; the unit at the value. A field whose source the measure cites is looked up there
    only. (spans, None) or (None, reason) - a literal that is not where it should be is never guessed."""
    sources = m.get("sources") or {}
    c0, c1 = clause(text, q0, q1)
    spans = {}
    for name, key in LITERALS:
        literal = m.get(key) or ""
        if not literal:
            continue
        if name in SOURCE_FIELDS and sources.get(name):
            at, why = in_source(text, q0, q1, name, literal, sources[name], cited, spans)
            if why:
                return None, why
        else:
            at = _find(text, q0, q1, c0, c1, name, literal)
            if at is None:
                return None, f"{name}_literal_not_located"
        spans[name] = (at, at + len(literal))
    if "period" in spans and not norm.period_span_whole_v5(text, *spans["period"]):
        # "2018" of "In January 2018" or "Since 2018" is part of another period (extraction_normalize.v5)
        return None, "period_literal_not_the_whole_period"
    if m.get("unit_text"):
        unit, why = _unit_span(text, m, spans)
        if unit is None:
            return None, why
        spans["unit"] = unit
    return spans, None


def context_start(text, pos):
    """Where the context of the text at pos begins: the topmost of its effective ancestor markdown headings (each
    above it of a lower level than the last one found) and the caption line above the table it is in."""
    starts = []
    line = text.rfind("\n", 0, pos) + 1
    if text.startswith("|", line):  # inside a table: the first non-table line above it is its caption
        i = line
        while i > 0:
            prev = text.rfind("\n", 0, i - 1) + 1
            if not text.startswith("|", prev):
                if text[prev:i - 1].strip():
                    starts.append(prev)
                break
            i = prev
    level, i = 7, line
    while i > 0:
        prev = text.rfind("\n", 0, i - 1) + 1
        m = re.match(r"(#{1,6})\s", text[prev:i - 1])
        if m and len(m.group(1)) < level:
            starts.append(prev)
            level = len(m.group(1))
        i = prev
    return min(starts, default=pos)


# ---- the request item ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Bound:
    """One measure bound to the fixed artifact it was read from: the subject BioCheck checks against, the quote's
    span in the artifact, and the producer named in the request item."""

    subject: dict
    quote_start: int
    quote_end: int
    measure: dict
    producer: dict


def _primary(text, b, base):
    """The primary passage's range: the clause, the effective headings and the 200 characters around every literal
    placed in it."""
    lo = max(0, min([b.quote_start] + [a for a, _ in base.values()]) - CONTEXT_WINDOW)
    hi = min(len(text), max([b.quote_end] + [e for _, e in base.values()]) + CONTEXT_WINDOW)
    lo = min(lo, context_start(text, b.quote_start))
    c0, c1 = clause(text, b.quote_start, b.quote_end)
    return min(lo, c0), max(hi, c1)


def _context_passage(text, passages, ranges, span, primary):
    """(id, None) of the context passage holding a cited context's span outside the primary passage - the cited
    range cut so it never overlaps the primary, shared by contexts with the same range - or (None, reason)."""
    (a, e), (lo, hi) = span, primary
    ra, rb = next((x, y) for x, y in ranges if x <= a and e <= y)
    ra, rb = (max(ra, hi), rb) if a >= hi else (ra, min(rb, lo))
    same = next((p for p in passages if (p["start"], p["end"]) == (ra, rb)), None)
    if same is None:
        if any(p["start"] < rb and ra < p["end"] for p in passages):
            return None, "cited_sources_overlap"
        same = {"id": f"c{len(passages)}", "role": "context", "start": ra, "end": rb, "text": text[ra:rb]}
        passages.append(same)
    return same["id"], None


def _passages(text, b, spans, sources, cited):
    """The primary passage and a context passage per cited context outside it (passages never overlap);
    (({name: passage id}, passages), None) or (None, reason)."""
    outer = {n for n in spans if sources.get(n) in CONTEXT_SOURCES}  # placed after the primary passage
    lo, hi = _primary(text, b, {n: v for n, v in spans.items() if n not in outer})
    if hi - lo > MAX_CHARS:
        return None, "context_over_20000_characters"
    passages = [{"id": "p1", "role": "primary", "start": lo, "end": hi, "text": text[lo:hi]}]
    where = {}
    for n in sorted(outer):
        a, e = spans[n]
        if lo <= a and e <= hi:
            where[n] = "p1"  # the context is inside the primary passage already: a span of it
            continue
        where[n], why = _context_passage(text, passages, cited[sources[n]], (a, e), (lo, hi))
        if why:
            return None, why
    if sum(len(p["text"]) for p in passages) > MAX_CHARS or len(passages) > 8:
        return None, "context_over_20000_characters"
    return (where, passages), None


def _fields(final, spans, where):
    def span(name):
        a, b = spans[name]
        return {"passage": where.get(name, "p1"), "start": a, "end": b}

    def stated(name, value):
        return {"state": "stated", "value": value, "span": span(name)} if name in spans and value else {
            "state": "unknown"}

    return {
        "object": stated("object", final["object"]),
        "metric": stated("metric", final["metric"]),
        "value": stated("value", final["value"]),
        "unit": stated("unit", final["unit"]),
        "period": ({"state": "stated", "value": final["period"], "span": span("period")}
                   if final["period"] and "period" in spans else {"state": "unknown"}),
        "scope": stated("scope", final["scope"]),
        "scope_mode": final["scope_mode"],
        "denominator": stated("denominator", final["denominator"]),
        "measure_kind": final["measure_kind"],
        "qualifier": final["qualifier"],
        "modality": final["modality"],
    }, span


def build(b, text, cited=None, dims_failed=None, item_id="m1"):
    """The contract-v3 request of one bound measure over the artifact text, with its final values, or (None,
    reason). cited: the sources its fields cite (cited_sources() and cited_dim_sources() together); a cited context
    outside the primary passage is a context passage of its own, one inside it is a span of the primary one."""
    m = b.measure
    final, why = final_of(m)
    if final is None:
        return None, why
    if not b.subject.get("raw_sha256"):
        return None, "raw_sha256_unknown"  # the subject binds the original by its hash: never sent without it
    if text[b.quote_start:b.quote_end] != m["quote"]:
        return None, "quote_not_at_its_place"
    spans, why = locate(text, b.quote_start, b.quote_end, m, cited)
    if spans is None:
        return None, why
    dims_at, dims_unlocated = locate_dims(text, b.quote_start, b.quote_end, m, cited, spans, dims_failed)
    sources = dict(m.get("sources") or {})
    for d in m.get("dims") or []:
        if d.get("kind") in dims_at:
            sources[f"dim:{d['kind']}"] = d.get("source") or "quote"
    spans = dict(spans, **{f"dim:{k}": v for k, v in dims_at.items()})
    placed, why = _passages(text, b, spans, sources, cited)
    if placed is None:
        return None, why
    where, passages = placed
    fields, span = _fields(final, spans, where)
    if final["measure_kind"] == "other":
        return None, "measure_kind_other_is_not_checkable"
    item = {"id": item_id, "kind": "measurement", "producer": b.producer,
            "locator": {"passage": "p1", "start": b.quote_start, "end": b.quote_end}, "quote": m["quote"],
            "fields": fields, "family": m.get("family") or "",
            "dims": [{"kind": k, "state": "stated", "value": text[a:e], "span": span(f"dim:{k}")}
                     for k, (a, e) in dims_at.items()]}
    body = {"contract_version": ec.CONTRACT_V3, "subject": b.subject, "passages": passages, "items": [item]}
    # the dimensions stated but not sendable stay with the final values, with why
    return (body, dict(final, dims_unlocated=dims_unlocated)), None
