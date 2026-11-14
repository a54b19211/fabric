"""Synthetic WHO prose: useful explicit counts and adversarial non-counts.

The eight real originals are evaluated separately in the isolated handoff;
these fixtures do not claim to be real WHO surveillance records.
"""

import json

import pytest
from bs4 import BeautifulSoup

from fetch.who_don import WHO_PARSER, project_who
from fetch.who_facts import extract_counts
from store.record_models import ArchiveRecord, VersionRecord


def projection(summary="", overview="", *, title="SYNTHETIC disease - Country A and Country B", **fields):
    data = {"Id": "00000000-0000-4000-8000-000000000001", "DonId": "SYNTHETIC-DON",
            "Title": title, "PublicationDate": "2030-01-10T00:00:00Z",
            "LastModified": "2030-01-11T00:00:00Z", "Summary": summary, "Overview": overview, **fields}
    item = ArchiveRecord(source_record_id=data["Id"], record_kind="document", raw_content=json.dumps(data))
    return data, project_who(item, data)


def assert_evidence(data, projected):
    for statement in projected["facts"]["count_statements"]:
        for key in ["evidence", "value_evidence"]:
            locator = statement[key]
            assert locator["basis"] == "clean_text"
            assert projected["clean_text"][locator["start"]:locator["end"]] == locator["quote"]
            native = data[locator["json_pointer"].lstrip("/")]
            assert locator["quote"] in BeautifulSoup(native, "html.parser").get_text(" ", strip=True)
        assert statement["assertion_status"] == "source_reported"
        assert statement["event_id"] is None
        assert statement["denominator"] is None and statement["rate"] is None


def test_explicit_word_counts_dates_metrics_and_html_positions():
    data, p = projection("<p>Between 1 January and 5 October 2026, Country A reported <b>eight</b> laboratory-confirmed cases of Disease X, including one death.</p>")
    rows = p["facts"]["count_statements"]
    assert [(r["value"], r["metric"]) for r in rows] == [(8, "case_count"), (1, "death_count")]
    assert rows[0]["case_status"] == "confirmed" and rows[1]["case_status"] is None
    assert rows[0]["unit"] == "cases" and rows[1]["unit"] == "deaths"
    assert all(r["count_kind"] == "report_in_period" for r in rows)
    assert all(r["time_scope"]["start"] == "2026-01-01" and r["time_scope"]["end"] == "2026-10-05" for r in rows)
    assert rows[0]["disease"] == "Disease X" and rows[1]["disease"] == "Disease X"
    assert rows[0]["geography"]["text"] == "Country A"
    assert_evidence(data, p)


def test_explicit_cumulative_count_and_no_imputed_observation_year():
    data, p = projection("As of 7 September, Country A has reported a cumulative total of 6,757 confirmed cases, including 3 267 deaths.")
    rows = p["facts"]["count_statements"]
    assert [r["value"] for r in rows] == [6757, 3267]
    assert all(r["count_kind"] == "cumulative" and r["time_scope"]["as_of"] is None for r in rows)
    assert all("calendar_bounds_incomplete_no_publication_date_imputation" in r["uncertainties"] for r in rows)
    assert p["source_published_at"]["value"].startswith("2030")
    assert_evidence(data, p)


def test_plain_report_does_not_default_to_cumulative_or_title_geography():
    _, p = projection("A total of five confirmed cases were reported.")
    row, = p["facts"]["count_statements"]
    assert row["count_kind"] == "unspecified"
    assert row["geography"] is None and row["disease"] is None
    assert row["time_scope"]["kind"] == "unknown"


def test_cumulative_cholera_count_retains_real_report_period_not_a_rate():
    data, p = projection("From 1 January to 10 July 2022, a cumulative number of 7796 cases of cholera including 37 associated deaths have been reported from 25 drought-affected districts.")
    rows = p["facts"]["count_statements"]
    assert [r["value"] for r in rows] == [7796, 37]
    assert all(r["count_kind"] == "cumulative" and r["disease"] == "cholera" for r in rows)
    assert rows[0]["time_scope"]["start"] == "2022-01-01"
    assert rows[0]["time_scope"]["end"] == "2022-07-10"
    assert not p["facts"]["count_extraction"]["rate_eligible"]
    assert_evidence(data, p)


def test_negative_new_cases_with_explicit_since_is_zero_with_open_end():
    data, p = projection("No new cases have been reported from South Kivu province since 29 May 2026.")
    row, = p["facts"]["count_statements"]
    assert row["value"] == 0 and row["metric"] == "case_count"
    assert row["time_scope"]["start"] == "2026-05-29" and row["time_scope"]["end"] is None
    assert row["geography"]["text"] == "South Kivu province"
    assert_evidence(data, p)


@pytest.mark.parametrize("wording", [
    "No new cases have been reported recently.",
    "No secondary cases have been reported to date.",
    "As of 9 October 2026, no cases have been reported.",
    "No further case of severe pneumonia was reported.",
    "There is no evidence of wider community transmission.",
    "Test results were negative for the tested high-threat pathogens.",
])
def test_unbounded_negative_or_no_evidence_does_not_become_zero(wording):
    _, p = projection(wording)
    assert not p["facts"]["count_statements"]
    assert p["facts"]["count_extraction"]["unparsed"]


def test_relative_interval_is_explicit_but_does_not_gain_absolute_dates():
    _, p = projection("During the preceding 24 hours, no new cases were reported in Region A.")
    row, = p["facts"]["count_statements"]
    assert row["value"] == 0
    assert row["time_scope"]["duration"] == {"value": 24, "unit": "hours"}
    assert row["time_scope"]["as_of"] is None


@pytest.mark.parametrize("wording", [
    "The authorities reported 50% vaccination coverage and 100 vaccine doses.",
    "A 60-year-old case was reported after a 14-day follow-up.",
    "A total of 677 stool samples were reported, and 145 samples tested positive.",
    "Authorities reported 30 contacts and 100 tests.",
    "The rate was reported as 100 cases per 100000 population.",
    "Authorities reported 10-20 cases.",
    "Authorities reported at least 30 cases.",
    "Authorities reported at most 30 cases.",
    "Authorities reported about 30 cases.",
    "Authorities reported roughly 30 cases.",
    "Authorities reported an estimated 30 cases.",
    "Authorities reported 5 to 10 cases.",
    "Authorities reported between 5 and 10 cases.",
    "Authorities reported five through ten cases.",
    "Authorities reported 3.5 cases.",
    "Authorities reported 5/100 cases.",
    "Authorities reported two cases were vaccinated and three vaccine doses were given.",
])
def test_doses_percentages_ages_tests_rates_ranges_and_vaccination_are_not_case_totals(wording):
    _, p = projection(wording)
    assert not p["facts"]["count_statements"]


def test_different_pathogens_keep_their_own_count_and_unknown_period():
    data, p = projection(overview="All results were reportedly negative for high-threat pathogens, and only two cases of COVID-19, three cases of rhinovirus, one case of Haemophilus spp., and other opportunistic infections were identified among contacts.", title="SYNTHETIC suspected plague - Country A")
    rows = p["facts"]["count_statements"]
    assert [(r["value"], r["disease"]) for r in rows] == [(2, "COVID-19"), (3, "rhinovirus"), (1, "Haemophilus spp")]
    assert all(r["time_scope"]["kind"] == "unknown" and r["geography"] is None for r in rows)
    assert not any(r["disease"] == "plague" for r in rows)
    assert_evidence(data, p)


def test_while_clauses_do_not_exchange_reporting_places():
    _, p = projection("Region A has reported three cases, while Region B has recorded two deaths.")
    rows = p["facts"]["count_statements"]
    assert [(r["value"], r["geography"]["text"]) for r in rows] == [(3, "Region A"), (2, "Region B")]


def test_supported_while_boundary_also_limits_disease_binding():
    _, p = projection("Country A reported three confirmed cases of cholera while Country B reported five confirmed cases of measles.")
    rows = p["facts"]["count_statements"]
    assert [(r["value"], r["disease"], r["geography"]["text"]) for r in rows] == [
        (3, "cholera", "Country A"), (5, "measles", "Country B"),
    ]


def test_report_update_is_not_incident_infections_or_backdated_cases():
    _, p = projection("Since the previous Disease Outbreak News was published, an additional 963 confirmed cases have been reported in Country A.")
    row, = p["facts"]["count_statements"]
    assert row["count_kind"] == "report_update" and row["time_scope"]["kind"] == "unknown"
    assert row["geography"]["text"] == "Country A"


def test_missing_year_in_range_and_notification_date_are_not_onset_dates():
    _, p = projection("From 5 to 11 February, three new confirmed cases were reported in Region A.",
                      "On 5 January 2023, the focal point notified WHO of one case of Disease X.")
    first, second = p["facts"]["count_statements"]
    assert first["time_scope"]["start"] is None and first["time_scope"]["end"] is None
    assert second["time_scope"]["as_of"] == "2023-01-05"
    assert second["time_scope"]["meaning"] == "notification_not_onset"


def test_repeated_counts_remain_separate_citations_and_background_is_not_extracted():
    text = "Country A reported two confirmed cases of Disease X."
    data, p = projection(text, text, Epidemiology="In 2007, Country B reported 30 cases.")
    rows = p["facts"]["count_statements"]
    assert len(rows) == 2 and [r["value"] for r in rows] == [2, 2]
    assert {r["evidence"]["json_pointer"] for r in rows} == {"/Summary", "/Overview"}
    assert_evidence(data, p)


def test_projection_retains_raw_native_locators_and_has_new_parser_version():
    data, p = projection("Country A reported eight confirmed cases.")
    assert WHO_PARSER == p["parser_version"] == "datafabric.who.don.projection.v0.2"
    assert p["facts"]["summary"] == data["Summary"]
    assert p["field_locators"]["summary"] == {"json_pointer": "/Summary"}
    # Validate the existing fabric record contract without widening its str-only locators.
    VersionRecord(source_id="who_don", source_record_id=data["Id"], version_id="a" * 64,
                  is_demo=True, raw_content=json.dumps(data), raw_sha256="b" * 64,
                  first_seen_at="2030-01-11T00:00:00Z", received_at="2030-01-11T00:00:00Z",
                  availability_proof={"kind": "unverified"}, published_seq=1, **p)


def test_misaligned_field_offset_fails_instead_of_fabricating_a_locator():
    with pytest.raises(ValueError):
        extract_counts({"Summary": ("Country A reported two cases.", 4)},
                       native_notice_id="synthetic", clean_text="unrelated")


def test_budget_is_explicit_and_does_not_claim_full_parse():
    text = "Country A reported two cases. Country A reported three deaths."
    value = extract_counts({"Summary": (text, 0)}, native_notice_id="synthetic",
                           clean_text=text, max_statements=1)
    assert len(value["statements"]) == 1 and value["truncated"]


def test_caption_date_does_not_bleed_into_next_paragraph():
    data, p = projection(overview="<p>Figure 4: Number of cases as of 21 October 2026</p><h2>Nigeria</h2><p>On 20 October 2026, Nigeria reported one laboratory-confirmed imported case of BVD.</p>")
    row, = p["facts"]["count_statements"]
    assert row["value"] == 1 and row["geography"]["text"] == "Nigeria"
    assert row["time_scope"]["as_of"] is None
    assert "Figure 4" not in row["evidence"]["quote"]
    assert_evidence(data, p)


def test_caption_cumulative_word_does_not_change_report_update():
    _, p = projection(overview="<p>Figure 1. Distribution of cumulative cases as of 7 September</p><p>Since the previous Disease Outbreak News was published, an additional 963 confirmed cases have been reported in Country A.</p>")
    row, = p["facts"]["count_statements"]
    assert row["count_kind"] == "report_update"
    assert row["time_scope"]["kind"] == "unknown"


@pytest.mark.parametrize("text", [
    "Region A has reported 5406 confirmed cases since the start of the outbreak, including 1114 new confirmed cases reported in the previous 21 days, as of 7 September.",
    "Region A has reported a cumulative number of 1066 confirmed cases, including 453 reported in the last 21 days.",
    "Region A has reported a cumulative number of 100 cases and five new cases.",
])
def test_mixed_total_and_recent_subcount_need_manual_time_binding(text):
    _, p = projection(text)
    assert not p["facts"]["count_statements"]
    assert set(p["facts"]["count_extraction"]["unparsed"][0]["reasons"]) & {
        "multiple_reporting_scopes_need_manual_binding", "multiple_reporting_frames_need_manual_binding",
    }


@pytest.mark.parametrize("text", [
    "Seven cases had underlying comorbidities, two reported direct camel contact, and one death was reported in an older adult.",
    "Two cases reported direct contact with camels while six reported no known exposure.",
    "Excludes n=163 cases for whom onset dates not reported.",
])
def test_exposure_and_missing_onset_subgroups_are_not_aggregate_case_counts(text):
    _, p = projection(text)
    assert not any(r["metric"] == "case_count" for r in p["facts"]["count_statements"])


@pytest.mark.parametrize("text", [
    "Country A reported one hundred and five confirmed cases.",
    "Country A reported twenty five confirmed cases.",
    "Country A has never reported five confirmed cases.",
    "Country A has not reported five confirmed cases.",
    "Country A reported not five confirmed cases but two cases.",
    "Country A no longer reported five confirmed cases.",
    "Country A reported three cases between 1 January and 5 January 2026 and Country B reported two cases between 1 February and 5 February 2026.",
    "Country A reported three confirmed cases of cholera and Country B reported five confirmed cases of measles.",
    "Country A reported three confirmed cases of cholera and five confirmed cases of measles.",
])
def test_unsupported_numbers_negation_or_multiple_frames_remain_unparsed(text):
    _, p = projection(text)
    assert not p["facts"]["count_statements"]
    assert p["facts"]["count_extraction"]["unparsed"]


def test_excluded_figure_does_not_fall_back_to_plain_body_text():
    data, p = projection("<figure><figcaption>As of 1 January 2026, Country A reported 12 cases.</figcaption></figure>")
    assert not p["facts"]["count_statements"]
    assert p["facts"]["count_extraction"]["unparsed"][0]["reasons"] == ["excluded_non_narrative_html"]
    assert "12 cases" in p["facts"]["summary"]
