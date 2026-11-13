"""Conservative source-reported counts from WHO DON prose, with exact text spans.

These are statements in a notice, not verified counts, independent events, rates,
or a complete surveillance series. Unsupported wording remains unparsed.
"""

import re
from datetime import date

METHOD = "who.explicit-count.v1"
FIELDS = ("Summary", "Overview")
WORDS = dict(zip(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split(),
    range(21),
))
NUMBER = r"(?:\d{1,3}(?:[,\u00a0\u202f ]\d{3})+|\d+|" + "|".join(WORDS) + r"|an?|no)"
QUALIFIER = r"(?:new|additional|laboratory-confirmed|confirmed|suspected|probable|associated|community|secondary|further|cumulative|imported|EVD|MERS-CoV)"
COUNT = re.compile(
    r"(?<![\w.%/–-])(?P<number>" + NUMBER + r")\s+"
    r"(?P<qualifiers>(?:" + QUALIFIER + r"\s+){0,5})(?P<unit>cases?|deaths?)\b", re.I,
)
REPORT = re.compile(r"\b(?:reported|recorded|notified|identified|registered|have reached|has reached)\b", re.I)
MONTH_NAMES = "January February March April May June July August September October November December".split()
MONTH = "(?:" + "|".join(MONTH_NAMES) + ")"
DATE = r"(?P<day>\d{1,2})\s+(?P<month>" + MONTH + r")(?:\s+(?P<year>\d{4}))?"
RANGE = re.compile(
    r"\b(?:between\s+|from\s+)?(?P<d1>\d{1,2})(?:\s+(?P<m1>" + MONTH + r"))?"
    r"(?:\s+(?P<y1>\d{4}))?\s+(?:to|and|–|-)\s+(?P<d2>\d{1,2})\s+"
    r"(?P<m2>" + MONTH + r")(?:\s+(?P<y2>\d{4}))?", re.I,
)
AS_OF = re.compile(r"\bas of\s+" + DATE, re.I)
SINCE = re.compile(r"\bsince\s+" + DATE, re.I)
ON = re.compile(r"\bon\s+" + DATE, re.I)
DURATION = re.compile(
    r"\b(?:(?:in|over|during|across)\s+)?(?:the\s+)?(?:past|last|previous|preceding|most recent)\s+"
    r"(?P<number>\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fourteen|twenty)"
    r"[ -](?P<unit>hours?|days?|weeks?)(?:\s+period)?", re.I,
)
SUBJECT = re.compile(
    r"(?P<name>[A-ZÀ-Ý][A-Za-zÀ-ÿ’'–-]*(?:\s+[A-Za-zÀ-ÿ’'–-]+){0,12}?)\s+"
    r"(?:(?:has|have)\s+)?(?:reported|recorded)\b",
)
SCOPE = re.compile(
    r"\b(?:(?:have|has|were|was)\s+)?(?:been\s+)?(?:reported|recorded|registered)\s+"
    r"(?:in|from|across)\s+(?P<name>[^.;:()]+)", re.I,
)


def _number(value):
    value = value.lower()
    if value in {"a", "an"}:
        return 1
    if value == "no":
        return 0
    return WORDS[value] if value in WORDS else int(re.sub(r"[,\s]", "", value))


def _iso(day, month, year):
    if not month or not year:
        return None
    try:
        return date(int(year), [m.lower() for m in MONTH_NAMES].index(month.lower()) + 1, int(day)).isoformat()
    except (ValueError, TypeError):
        return None


def _time(text):
    result = {"kind": "unknown", "raw": None, "start": None, "end": None,
              "as_of": None, "duration": None, "meaning": "source_reporting_scope",
              "explicit_interval": False}
    interval = RANGE.search(text)
    if interval:
        g = interval.groupdict()
        start = _iso(g["d1"], g["m1"] or g["m2"], g["y1"] or g["y2"])
        end = _iso(g["d2"], g["m2"], g["y2"] or g["y1"])
        if start and end and start > end:
            start = end = None
        result.update(kind="reporting_interval", raw=interval.group(), start=start, end=end,
                      explicit_interval=True)
        return result
    since = SINCE.search(text)
    if since:
        result.update(kind="since", raw=since.group(),
                      start=_iso(**since.groupdict()), explicit_interval=True)
        return result
    duration = DURATION.search(text)
    if duration:
        result.update(kind="relative_reporting_interval", raw=duration.group(),
                      duration={"value": _number(duration["number"]), "unit": duration["unit"].lower()},
                      explicit_interval=True)
    as_of = AS_OF.search(text)
    if as_of:
        result.update(as_of=_iso(**as_of.groupdict()))
        if not duration:
            result.update(kind="as_of", raw=as_of.group())
        else:
            result["raw"] += "; " + as_of.group()
    if result["kind"] == "unknown":
        notification = ON.search(text)
        if notification and re.search(r"\bnotified\s+WHO\b", text, re.I):
            result.update(kind="notification_date", raw=notification.group(),
                          as_of=_iso(**notification.groupdict()), meaning="notification_not_onset")
    return result


def _sentences(text):
    start = 0
    for stop in [m.end() for m in re.finditer(r"[.!?](?=\s+[A-Z]|$)", text)] + [len(text)]:
        while start < stop and text[start].isspace():
            start += 1
        end = stop
        while end > start and text[end - 1].isspace():
            end -= 1
        if start < end:
            yield start, end, text[start:end]
        start = stop


def _block_sentences(text, blocks):
    for a, b in ([(0, len(text))] if blocks is None else blocks):
        block = text[a:b]
        if re.match(r"\s*(?:Figure|Table)\s*\d*\s*[.:]", block, re.I):
            continue
        for start, end, sentence in _sentences(block):
            yield a + start, a + end, sentence


def _clause(text, position):
    boundaries = list(re.finditer(r";|,?\s+while\s+|,?\s+whereas\s+", text, re.I))
    start = max([m.end() for m in boundaries if m.end() <= position], default=0)
    end = min([m.start() for m in boundaries if m.start() > position], default=len(text))
    return text[start:end], position - start


def _disease(text, match):
    qualifiers = match["qualifiers"]
    for token in ["EVD", "MERS-CoV"]:
        if token.lower() in qualifiers.lower():
            return token
    tail = text[match.end():]
    direct = re.match(r"\s+of\s+(.+?)(?=,|;|:|\s+including\b|\s+(?:was|were|has|have|in|from|among)\b|$)", tail, re.I)
    if direct:
        value = direct[1].strip().rstrip(".")
        if value and len(value) <= 100 and value.lower() not in {"which", "these", "them", "the cases"}:
            return value
    return None


def _geography(text, position):
    own = COUNT.match(text, position)
    if own:
        place = re.match(r"\s+(?:in|from)\s+(.+?)(?=,|;|\s+and\b|\s+including\b|\s+(?:since|during|as of)\b|$)", text[own.end():], re.I)
        if place and re.search(r"[A-ZÀ-Ý]", place[1]):
            return {"text": place[1].strip().rstrip("."), "basis": "same_count_explicit_place"}
    candidates = [m for m in SUBJECT.finditer(text) if m.end() <= position]
    if candidates:
        name = candidates[-1]["name"].strip()
        # Do not turn a reporting institution or a generic pronoun into an affected place.
        if name.lower() not in {"the country", "country", "this country"} and not re.search(r"\b(?:WHO|Ministry|authorities|NFP|Health|Institute|cases?|deaths?|Overall|has|have|never|were|was|been|including|no|new|confirmed|total)\b", name, re.I):
            return {"text": name, "basis": "same_clause_reporting_subject"}
    scopes = list(SCOPE.finditer(text))
    if len(scopes) == 1:
        name = re.split(r"\s+(?:since|during|as of|over the|between|with)\b", scopes[0]["name"], maxsplit=1, flags=re.I)[0].strip(" ,")
        if re.match(r"(?:the\s+)?(?:preceding|previous|last|past|most recent)\b", name, re.I):
            place = re.search(r"\b(?:from|across)\s+(.+)$", name, re.I)
            name = place[1] if place else ""
        if name and len(name) <= 250 and re.search(r"[A-ZÀ-Ý]", name):
            return {"text": name, "basis": "same_clause_explicit_reported_scope"}
    return None


def _kind(text, match, period, position):
    if re.search(r"\bcumulative\b", text, re.I):
        return "cumulative"
    if re.search(r"\bsince\s+(?:the\s+)?(?:previous|last)\b", text, re.I):
        return "report_update"
    if period["explicit_interval"]:
        return "report_in_period"
    if re.search(r"\b(?:new|additional)\b", match["qualifiers"] + text[:position], re.I):
        return "reported_new"
    return "unspecified"


def _framing_reason(clause, before):
    """Compound numbers and clauses with several reporting frames or scopes cannot be bound without a human."""
    clause_counts = list(COUNT.finditer(clause))
    if re.search(r"\b(?:hundred|thousand|million|billion)\b", clause, re.I) or re.search(
        r"\b(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)\s*$", before, re.I,
    ):
        return "unsupported_compound_number"
    if ((len(clause_counts) > 1 and len(REPORT.findall(clause)) > 1)
            or len(RANGE.findall(clause)) > 1 or len(AS_OF.findall(clause)) > 1
            or len(DURATION.findall(clause)) > 1
            or any(COUNT.search(disease) for other in clause_counts
                   if (disease := _disease(clause, other)) is not None)):
        return "multiple_reporting_frames_need_manual_binding"
    included = re.search(r"\bincluding\b", clause, re.I)
    mixed_new = re.search(r"\bcumulative\b", clause, re.I) and any(
        re.search(r"\b(?:new|additional)\b", other["qualifiers"], re.I)
        for other in COUNT.finditer(clause)
    )
    if mixed_new or (included and COUNT.search(clause[:included.start()]) and (
        DURATION.search(clause[included.end():]) or RANGE.search(clause[included.end():])
    )):
        return "multiple_reporting_scopes_need_manual_binding"
    return None


def _context_reason(clause, match, period, before, after):
    """Negated, subgroup, rate/range/approximate and vaccination wording, or no reporting statement at all."""
    if re.search(r"\b(?:excludes?|excluded|not|never)\b|\bno longer\b", clause, re.I):
        return "excluded_or_negated_reporting_context"
    if re.match(r"\s+(?:(?:had|have|were|are)\s+(?:underlying|comorbidities|symptomatic)|reported\s+(?:direct|no known|contact))\b", after, re.I):
        return "clinical_or_exposure_subgroup_not_aggregate_count"
    if re.match(r"\s*(?:per\b|/|for every\b)", after, re.I) or re.search(
        r"(?:\d\s*[–-]|at least|at most|more than|fewer than|less than|up to|approximately|around|about|roughly|nearly|estimated(?: at)?|\b(?:" + NUMBER + r")\s+(?:to|through|and))\s*$", before, re.I,
    ):
        return "rate_range_or_approximate_count_not_supported"
    if re.search(r"\b(?:vaccinated|vaccination|doses?|received\s+(?:the\s+)?(?:vaccine|OCV))\b", after, re.I):
        return "vaccination_or_treatment_subset_not_aggregate_count"
    negative = match["number"].lower() in {"no", "zero"} or _number(match["number"]) == 0
    if negative and not period["explicit_interval"]:
        return "negative_count_without_explicit_interval"
    if not REPORT.search(clause):
        return "no_explicit_aggregate_reporting_statement"
    return None


def _rejection_reason(clause, match, period, before, after):
    """Why a count in `clause` is not a supported explicit aggregate statement; None when it is. The first rule wins."""
    return _framing_reason(clause, before) or _context_reason(clause, match, period, before, after)


def _bound_disease(clause, local, metric):
    disease = _disease(clause, COUNT.match(clause, local))
    if disease is None and metric == "death_count":
        # An explicitly included death may share one named case population in this clause.
        named = {_disease(clause, other) for other in COUNT.finditer(clause)
                 if other["unit"].lower().startswith("case")}
        named.discard(None)
        if len(named) == 1 and re.search(r"\bincluding\b", clause, re.I):
            disease = named.pop()
    return disease


def _count_row(clause, local, match, period, after, evidence, offset, start, native_notice_id):
    """The statement row of one supported count, with what the clause leaves unbound listed as uncertainties."""
    included = re.search(r"\bincluding\b", clause, re.I)
    metric = "death_count" if match["unit"].lower().startswith("death") or re.match(r"\s+died\b", after, re.I) else "case_count"
    disease = _bound_disease(clause, local, metric)
    geography = _geography(clause, local)
    qualifiers = match["qualifiers"].strip().lower().split()
    status = next((word for word in ["laboratory-confirmed", "confirmed", "suspected", "probable"] if word in qualifiers), "not_specified")
    if status == "laboratory-confirmed":
        status = "confirmed"
    unknown = []
    if period["kind"] == "unknown":
        unknown.append("observation_period_unknown")
    elif not (period["as_of"] or (period["start"] and period["end"])):
        unknown.append("calendar_bounds_incomplete_no_publication_date_imputation")
    if geography is None:
        unknown.append("geography_not_explicitly_bound")
    if disease is None:
        unknown.append("disease_not_explicitly_bound")
    return {"metric": metric, "value": _number(match["number"]),
            "unit": "cases" if metric == "case_count" else "deaths",
            "case_status": status if metric == "case_count" else None,
            "qualifiers": qualifiers, "count_kind": _kind(clause, match, period, local),
            "count_role": "included_subgroup" if included and local > included.end() else "statement_count",
            "time_scope": period, "geography": geography, "disease": disease,
            "event_id": None, "native_notice_id": native_notice_id,
            "assertion_status": "source_reported", "uncertainties": unknown,
            "evidence": evidence,
            "value_evidence": {**evidence, "start": offset + start + match.start("number"),
                               "end": offset + start + match.end("number"), "quote": match["number"]},
            "denominator": None, "rate": None, "method_version": METHOD}


def _scan_sentence(sentence, evidence, offset, start, native_notice_id, statements, unparsed, max_statements):
    """Append the supported counts of one sentence to `statements`; True when the statement cap was hit."""
    truncated = False
    emitted, reasons = 0, set()
    for match in COUNT.finditer(sentence):
        clause, local = _clause(sentence, match.start())
        period = _time(clause)
        before = sentence[max(0, match.start() - 30):match.start()]
        after = sentence[match.end():match.end() + 75]
        reason = _rejection_reason(clause, match, period, before, after)
        if reason:
            reasons.add(reason)
            continue
        row = _count_row(clause, local, match, period, after, evidence, offset, start, native_notice_id)
        if len(statements) >= max_statements:
            truncated = True
            break
        statements.append(row)
        emitted += 1
    if len(unparsed) < 50 and (reasons or (not emitted and re.search(r"\bcases?\b|\bdeaths?\b|\bnegative\b|\bno evidence\b", sentence, re.I))):
        unparsed.append({"evidence": evidence, "reasons": sorted(reasons or {"no_supported_explicit_count_statement"})})
    return truncated


def extract_counts(sections, *, native_notice_id, clean_text, max_statements=100):
    """sections maps native field names to (rendered text, offset in clean_text).

    Source field locators use JSON pointers. All character spans explicitly use
    stored clean_text, never pretend to be offsets in HTML or JSON bytes.
    """
    statements, unparsed, truncated = [], [], False
    for field in FIELDS:
        if field not in sections:
            continue
        section = sections[field]
        text, offset = section[:2]
        blocks = section[2] if len(section) > 2 else None
        if clean_text[offset:offset + len(text)] != text:
            raise ValueError("WHO source field does not match frozen clean_text")
        if blocks == [] and text and len(unparsed) < 50:
            unparsed.append({"evidence": {"json_pointer": "/" + field, "basis": "clean_text",
                                         "start": offset, "end": offset + len(text), "quote": text},
                             "reasons": ["excluded_non_narrative_html"]})
        for start, end, sentence in _block_sentences(text, blocks):
            evidence = {"json_pointer": "/" + field, "basis": "clean_text",
                        "start": offset + start, "end": offset + end, "quote": sentence}
            assert clean_text[evidence["start"]:evidence["end"]] == sentence
            truncated = _scan_sentence(sentence, evidence, offset, start, native_notice_id,
                                       statements, unparsed, max_statements)
            if truncated:
                break
        if truncated:
            break
    return {"statements": statements, "unparsed": unparsed, "method_version": METHOD,
            "assertion_status": "source_reported", "fields": ["/" + name for name in FIELDS if name in sections],
            "truncated": truncated, "denominator_available": False, "rate_eligible": False,
            "limitations": ["Statements are not independently verified or independent outbreak events.",
                            "Repeated counts in sections or notices must not be added or treated as a trend.",
                            "Only supported explicit wording is parsed; unknown is not zero.",
                            "Publication dates never fill missing observation dates or years."]}
