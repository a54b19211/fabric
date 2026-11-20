"""The content fingerprint that decides whether an original is a new version (store.records.fingerprint)."""

import json

from store.records import fingerprint


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


def test_a_who_media_link_written_as_the_null_placeholder_is_not_a_change():
    """Live 2026-10-23..11-15: 2024-DON503 alternated between the two writings of one link, LastModified unchanged,
    and got 14 versions. The link is compared by its sfvrsn (the version of the media item); the text still counts."""
    def raw(link, text="WHO. Terms of Reference"):
        return json.dumps({"Id": "322c49c3", "LastModified": "2026-10-23T12:53:04Z",
                           "FurtherInformation": '<li><a href="' + link + '">' + text + "</a></li>"})

    real = "https://cdn.who.int/media/docs/default-source/influenza/nic_tor_en.pdf?sfvrsn=93513e78_30"
    placeholder = "https://cdn.who.int/media/00000000-0000-0000-0000-000000000000?sfvrsn=93513e78_30"
    assert fingerprint("who_don", raw(real)) == fingerprint("who_don", raw(placeholder))
    assert fingerprint("who_don", raw(real)) != fingerprint("who_don", raw(real.replace("_30", "_31")))  # new file
    assert fingerprint("who_don", raw(real)) != fingerprint("who_don", raw(real, "WHO. Terms of Reference (2024)"))
    assert fingerprint("europe_pmc", raw(real)) != fingerprint("europe_pmc", raw(placeholder))  # only for WHO


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


def test_pubmed_history_order_and_ct_year_only_nih_ids_are_not_changes():
    """PubMed 每次给的 <History> 日期顺序不固定；CT.gov 自动挂的“编号只是年份”的 NIH 次要编号时有时无：都不算新版本。"""
    a = ('<PubmedArticle><History><PubMedPubDate PubStatus="received"><Year>2025</Year></PubMedPubDate>'
         '<PubMedPubDate PubStatus="medline"><Year>2026</Year></PubMedPubDate></History></PubmedArticle>')
    b = ('<PubmedArticle><History><PubMedPubDate PubStatus="medline"><Year>2026</Year></PubMedPubDate>'
         '<PubMedPubDate PubStatus="received"><Year>2025</Year></PubMedPubDate></History></PubmedArticle>')
    assert fingerprint("pubmed", a) == fingerprint("pubmed", b)
    assert fingerprint("pubmed", a) != fingerprint("pubmed", a.replace("2025", "2024"))
    real = {"id": "R01CA1", "type": "NIH", "link": "x"}
    year = {"id": "2025", "type": "NIH", "link": "https://reporter.nih.gov/quickSearch/2025"}
    def ct(ids):
        return json.dumps({"protocolSection": {"identificationModule": {"nctId": "NCT1", "secondaryIdInfos": ids}}})
    assert fingerprint("clinicaltrials", ct([real, year])) == fingerprint("clinicaltrials", ct([real]))
    assert fingerprint("clinicaltrials", ct([real])) != fingerprint("clinicaltrials", ct([]))
