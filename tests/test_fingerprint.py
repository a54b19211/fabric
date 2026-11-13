"""The content fingerprint that decides whether an original is a new version (store.history.fingerprint)."""

import json

from store.history import fingerprint


def test_key_order_and_spacing_are_not_a_change_but_values_and_array_order_are():
    """Live 2026-11-03: Europe PMC returned the same records with their keys in another order (23927 versions for
    8444 records)."""
    record = {"id": "123", "title": "Alpha", "authorList": {"author": [{"fullName": "A"}, {"fullName": "B"}]}}
    reordered = json.dumps(dict(reversed(list(record.items()))), indent=2)
    assert fingerprint("europe_pmc", json.dumps(record)) == fingerprint("europe_pmc", reordered)
    assert fingerprint("europe_pmc", json.dumps(record)) != fingerprint(
        "europe_pmc", json.dumps(dict(record, title="Alpha (corrected)")))
    swapped = dict(record, authorList={"author": [{"fullName": "B"}, {"fullName": "A"}]})
    assert fingerprint("europe_pmc", json.dumps(record)) != fingerprint("europe_pmc", json.dumps(swapped))


def test_the_clinicaltrials_data_date_is_not_a_change():
    """Live 2026-11-03: every refresh made a new version of all 516 trials; only versionHolder differed."""
    def raw(holder, status="RECRUITING"):
        return json.dumps({"protocolSection": {"statusModule": {"overallStatus": status}},
                           "derivedSection": {"miscInfoModule": {"versionHolder": holder}}})

    assert fingerprint("clinicaltrials", raw("2026-11-02")) == fingerprint("clinicaltrials", raw("2026-11-03"))
    assert fingerprint("clinicaltrials", raw("2026-11-02")) != fingerprint("clinicaltrials", raw("2026-11-02", "COMPLETED"))
    assert fingerprint("europe_pmc", raw("2026-11-02")) != fingerprint("europe_pmc", raw("2026-11-03"))  # only CT's


def capture(html, markdown="# T\n\nbody", attachments=(), **extra):
    return json.dumps({"schema_version": "datafabric.web_capture.v1", "role": "web_capture", "site": "nmpa",
                       "url": "https://x/a.html", "html": html, "html_sha256": "h" + html,
                       "parsed": {"title": "T", "markdown": markdown},
                       "attachments": [{"record_id": "r" + u, "url": u} for u in attachments], **extra})


def test_a_web_capture_is_compared_by_what_was_parsed_and_its_attachment_list_not_its_html():
    """NMPA writes new widget ids and ChiCTR new download paths into every page."""
    assert fingerprint("nmpa", capture("<a id=1>")) == fingerprint("nmpa", capture("<a id=2>"))
    assert fingerprint("nmpa", capture("<a>")) == fingerprint("nmpa", capture("<b>", html_store_path="/d/h.html"))
    assert fingerprint("nmpa", capture("<a>")) != fingerprint("nmpa", capture("<a>", markdown="# T\n\nbody 2"))
    assert fingerprint("nmpa", capture("<a>")) != fingerprint("nmpa", capture("<a>", attachments=("u1",)))


def test_an_original_that_is_not_json_is_compared_as_it_is():
    assert fingerprint("pubmed", "<PubmedArticle/>") == fingerprint("pubmed", "<PubmedArticle/>")
    assert fingerprint("pubmed", "<PubmedArticle/>") != fingerprint("pubmed", "<PubmedArticle />")
