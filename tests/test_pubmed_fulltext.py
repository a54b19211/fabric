"""PubMed 合法全文（fetch.pubmed_fulltext）：PMC JATS / Unpaywall OA PDF 作为附件版本，或如实记缺失。

不联网：httpx.MockTransport 替身；每个请求都要先过共享限速（rate.wait 替身记次数）。
"""

import json
from pathlib import Path

import httpx
import pytest
from pdf_fixture import make_pdf

from clean import attachment_parse as ap
from clean import jats_text
from fetch import pubmed_fulltext as pf
from fetch import rate, sources, transport
from fetch import web_client as client
from store.history_models import FetchRequest

EMAIL = "ops-mailbox@example.invalid"
OA_PDF = "https://repo.example.org/b.pdf"


def article(pmid, doi, pmcid=None):
    ids = f'<ArticleId IdType="pubmed">{pmid}</ArticleId><ArticleId IdType="doi">{doi}</ArticleId>'
    if pmcid:
        ids += f'<ArticleId IdType="pmc">{pmcid}</ArticleId>'
    return (
        f'<PubmedArticle><MedlineCitation Status="MEDLINE" Owner="NLM"><PMID Version="1">{pmid}</PMID>'
        f"<Article><Journal><JournalIssue><PubDate><Year>2020</Year></PubDate></JournalIssue></Journal>"
        f"<ArticleTitle>Synthetic {pmid}</ArticleTitle><Abstract><AbstractText>Abstract {pmid}.</AbstractText>"
        f"</Abstract></Article></MedlineCitation><PubmedData><ArticleIdList>{ids}</ArticleIdList></PubmedData>"
        f"</PubmedArticle>"
    )


ARTICLES = {"111": article("111", "10.1000/a", "PMC111"), "222": article("222", "10.1000/b")}
PUBMED = '<?xml version="1.0" ?><PubmedArticleSet>' + "".join(ARTICLES.values()) + "</PubmedArticleSet>"
REAL_CLIENT = httpx.Client


def jats(license_xml, body=True, pmcid="PMC111"):
    body_xml = (
        "<body><sec><label>1</label><title>Introduction</title><p>PD-1 blockade text.</p>"
        "<table-wrap><label>Table 1</label><caption><p>Doses</p></caption><table><tr><th>Arm</th><th>Dose</th></tr>"
        "<tr><td>A</td><td>3 mg/kg</td></tr></table></table-wrap></sec>"
        "<sec><label>2</label><title>Methods</title><sec><title>Patients</title><p>Ten patients.</p></sec></sec>"
        "</body>" if body else ""
    )
    return (
        '<?xml version="1.0" ?><!DOCTYPE pmc-articleset PUBLIC "-//NLM//DTD ARTICLE SET 2.0//EN" '
        '"https://dtd.nlm.nih.gov/ncbi/pmc/articleset/nlm-articleset-2.0.dtd"><pmc-articleset>'
        '<article xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:ali="http://www.niso.org/schemas/ali/1.0/">'
        f'<front><article-meta><article-id pub-id-type="pmcid">{pmcid}</article-id>'
        "<title-group><article-title>Synthetic full text</article-title></title-group>"
        f"<permissions>{license_xml}</permissions><abstract><p>Abstract A.</p></abstract></article-meta></front>"
        f"{body_xml}<back><ref-list><ref><mixed-citation>Ref text</mixed-citation></ref></ref-list></back>"
        "</article></pmc-articleset>"
    ).encode()


CC_BY = ('<license license-type="open-access" xlink:href="https://creativecommons.org/licenses/by/4.0/">'
         "<license-p>CC BY</license-p></license>")
UNCLEAR = '<license license-type="open-access"><license-p>Free to read.</license-p></license>'


class Web:
    def __init__(self, monkeypatch, *, pmc=b"", pmc_status=200, unpaywall=None):
        self.calls, self.gates = [], []
        self.pmc, self.pmc_status, self.unpaywall = pmc, pmc_status, unpaywall or {}
        monkeypatch.setattr(rate, "wait", lambda group, interval: self.gates.append(group))
        monkeypatch.setattr(httpx, "Client",
                            lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(self.handle), **kw))

    def handle(self, request):
        self.calls.append(request)
        url, params = request.url, dict(request.url.params)
        if url.path.endswith("efetch.fcgi") and params.get("db") == "pubmed":
            asked = params["id"].split(",")
            return httpx.Response(200, text='<?xml version="1.0" ?><PubmedArticleSet>'
                                  + "".join(ARTICLES[pmid] for pmid in asked) + "</PubmedArticleSet>")
        if url.path.endswith("efetch.fcgi") and params.get("db") == "pmc":
            return httpx.Response(self.pmc_status, content=self.pmc)
        if url.host == "api.unpaywall.org":
            doi = url.path[len("/v2/"):]
            if doi not in self.unpaywall:
                return httpx.Response(404, json={"error": True})
            return httpx.Response(200, json=self.unpaywall[doi])
        if str(url) == OA_PDF:
            return httpx.Response(200, content=make_pdf("OA PDF"), headers={"content-type": "application/pdf"})
        return httpx.Response(404)

    def read(self, **extra):
        return sources.read_public(
            FetchRequest(source_id="pubmed", query="x", source_record_ids=["111", "222"], **extra))


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(client.APP, "DATA_ROOT", tmp_path)
    monkeypatch.delenv("DATAFABRIC_UNPAYWALL_EMAIL", raising=False)
    monkeypatch.delenv("DATAFABRIC_PUBMED_FULLTEXT", raising=False)


def by_route(batch):
    return {(c["parent_record_id"], c.get("route")): c for c in batch["attachment_coverage"]}


def test_pmc_cc_fulltext_becomes_an_attachment_version_and_parses_with_section_locators(monkeypatch, tmp_path):
    web = Web(monkeypatch, pmc=jats(CC_BY))
    batch = web.read()
    assert batch["error"] is None
    # 每个对外请求都先过共享限速，并计入本次读取的请求数
    assert len(web.gates) == len(web.calls) == batch["requests"] == 2
    assert set(web.gates) == {"ncbi.nlm.nih.gov"}
    articles = [r for r in batch["records"] if r.raw_media_type == "application/xml"]
    [attachment] = [r for r in batch["records"] if r.raw_media_type == "application/json"]
    assert [a.source_record_id for a in articles] == ["111", "222"]
    assert attachment.source_record_id == "111#attachment=" + pf.pmc_url("PMC111")
    projected = sources.project_record("pubmed", attachment)
    facts = projected["facts"]
    assert facts["role"] == "attachment" and facts["parent_record_id"] == "111" and facts["file_ext"] == ".xml"
    assert facts["access"]["tier"] == "pmc_fulltext_cc" and facts["access"]["license"] == "CC BY 4.0"
    assert Path(facts["store_path"]).read_bytes() == jats(CC_BY)
    assert Path(facts["store_path"]).parent == tmp_path / "raw" / "pubmed"
    coverage = by_route(batch)
    assert coverage[("111", "pmc")]["status"] == "stored"
    assert coverage[("222", "pmc")]["status"] == "no_pmcid"
    assert coverage[("222", "unpaywall")]["status"] == "unpaywall_not_configured"  # 没配邮箱：跳过并记缺口
    assert batch["bytes"] > len(PUBMED)

    parsed = ap.parse_file(store_path=facts["store_path"], sha256=facts["sha256"], file_ext=facts["file_ext"])
    assert parsed["status"] == "parsed" and parsed["parse_method"] == "jats_xml"
    assert parsed["locator_kind"] == "jats_section"
    text = parsed["text"]
    for segment in parsed["segments"]:
        assert text[segment["start"]:segment["end"]].strip() == text[segment["start"]:segment["end"]]
    para = next(s for s in parsed["segments"] if text[s["start"]:s["end"]] == "Ten patients.")
    assert para["section"] == "2 Methods > Patients" and para["jats_path"] == "body/sec[2]/sec[1]/p[1]"
    table = next(s for s in parsed["segments"] if s["type"] == "table")
    assert "A | 3 mg/kg" in text[table["start"]:table["end"]]
    assert "Ref text" not in text  # 参考文献不进正文


def test_pmc_without_body_is_a_missing_branch_and_unpaywall_unclear_license_is_not_downloaded(monkeypatch):
    monkeypatch.setenv("DATAFABRIC_UNPAYWALL_EMAIL", EMAIL)
    web = Web(monkeypatch, pmc=jats(CC_BY, body=False), unpaywall={
        "10.1000/a": {"is_oa": True, "oa_status": "bronze", "best_oa_location": {
            "url_for_pdf": "https://publisher.example.org/a.pdf", "license": "publisher-specific-oa",
            "host_type": "publisher"}},
        "10.1000/b": {"is_oa": False, "oa_status": "closed", "best_oa_location": None, "oa_locations": []},
    })
    batch = web.read()
    assert [r.raw_media_type for r in batch["records"]] == ["application/xml", "application/xml"]
    coverage = by_route(batch)
    assert coverage[("111", "pmc")]["status"] == "pmc_fulltext_not_available"
    unclear = coverage[("111", "unpaywall")]
    assert unclear["status"] == "oa_license_unclear" and unclear["license"] == "publisher-specific-oa"
    assert unclear["url"] == "https://publisher.example.org/a.pdf"
    assert coverage[("222", "unpaywall")]["status"] == "unpaywall_no_oa_pdf"
    assert not any("publisher.example.org" in str(c.url) for c in web.calls)  # 许可不明：不下载
    unpaywall_calls = [c for c in web.calls if c.url.host == "api.unpaywall.org"]
    assert all(dict(c.url.params)["email"] == EMAIL for c in unpaywall_calls)
    assert EMAIL not in json.dumps(batch["attachment_coverage"])  # 邮箱只进请求，不进覆盖记录
    assert len(web.gates) == len(web.calls) == batch["requests"]


def test_body_with_unclear_license_or_wrong_article_is_not_saved(monkeypatch):
    batch = Web(monkeypatch, pmc=jats(UNCLEAR)).read()
    assert by_route(batch)[("111", "pmc")]["status"] == "pmc_license_unclear"
    assert len(batch["records"]) == 2
    batch = Web(monkeypatch, pmc=jats(CC_BY, pmcid="PMC999")).read()
    assert by_route(batch)[("111", "pmc")]["status"] == "pmc_id_mismatch"
    batch = Web(monkeypatch, pmc=b"busy", pmc_status=503).read()
    assert by_route(batch)[("111", "pmc")]["status"] == "pmc_fetch_failed"
    assert batch["error"] is None and len(batch["records"]) == 2  # 全文失败不让元数据失败


def test_unpaywall_cc_pdf_is_downloaded_through_the_shared_store(monkeypatch, tmp_path):
    monkeypatch.setenv("DATAFABRIC_UNPAYWALL_EMAIL", EMAIL)
    web = Web(monkeypatch, pmc=jats(CC_BY, body=False), unpaywall={
        "10.1000/b": {"is_oa": True, "oa_status": "gold", "best_oa_location": {
            "url_for_pdf": OA_PDF, "license": "cc-by", "host_type": "repository", "version": "publishedVersion"}},
    })
    batch = web.read()
    [attachment] = [r for r in batch["records"] if r.raw_media_type == "application/json"]
    assert attachment.source_record_id == "222#attachment=" + OA_PDF
    facts = sources.project_record("pubmed", attachment)["facts"]
    assert facts["access"]["tier"] == "unpaywall_oa_pdf" and facts["access"]["license"] == "cc-by"
    assert facts["file_ext"] == ".pdf" and Path(facts["store_path"]).read_bytes() == make_pdf("OA PDF")
    assert Path(facts["store_path"]).parent == tmp_path / "raw" / "pubmed"
    assert by_route(batch)[("222", "unpaywall")]["status"] == "stored"
    # 下载也先过闸：PubMed、PMC、两次 Unpaywall 查询（a 无记录 404）、PDF
    assert len(web.gates) == batch["requests"] == len(web.calls) == 5
    assert web.gates.count("repo.example.org") == 1


def test_full_text_takes_a_version_slot_and_the_read_resumes_where_it_stopped(monkeypatch):
    """Codex 400 第 4 点：max_records 按本次产出的版本计，文献和全文都算；额度不足给续读位置，不越界、不丢。"""
    from store.history_models import FetchContinuation

    web = Web(monkeypatch, pmc=jats(CC_BY))
    ids = ["111", "222"]
    first = web.read(max_records=1)
    assert [r.source_record_id for r in first["records"]] == ["111"] and first["stop_reason"] == "record_budget"
    assert json.loads(first["next_continuation"]["cursor"]) == {"i": 0, "a": 1}  # 第 0 篇已存，只差全文
    second = sources.read_public(FetchRequest(
        source_id="pubmed", query="x", source_record_ids=ids, max_records=1,
        continuation=FetchContinuation(**first["next_continuation"])))
    assert [r.source_record_id for r in second["records"]] == ["111#attachment=" + pf.pmc_url("PMC111")]
    assert json.loads(second["next_continuation"]["cursor"]) == {"i": 1, "a": 0}
    third = sources.read_public(FetchRequest(
        source_id="pubmed", query="x", source_record_ids=ids, max_records=1,
        continuation=FetchContinuation(**second["next_continuation"])))
    assert [r.source_record_id for r in third["records"]] == ["222"]
    assert third["upstream_pagination_exhausted"] and third["next_continuation"] is None


def test_deadline_stops_before_any_request(monkeypatch):
    from types import SimpleNamespace

    web = Web(monkeypatch, pmc=jats(CC_BY))
    clock = iter([0.0] + [10_000.0] * 100)  # 读取在 0 秒开始；等完限速再看表，已经过了截止时间
    monkeypatch.setattr(transport, "time", SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda s: None))
    batch = web.read()
    assert batch["stop_reason"] == "deadline" and batch["records"] == [] and not web.calls
    assert json.loads(batch["next_continuation"]["cursor"]) == {"i": 0, "a": 0}


def test_license_terms_are_kept_per_article():
    assert pf.conditions("CC BY-NC 4.0") == ["BY", "NC"] and pf.conditions("cc-by-nc-nd") == ["BY", "NC", "ND"]
    assert pf.conditions("CC0 1.0") == [] and pf.conditions("cc-by-sa") == ["BY", "SA"]


def test_fulltext_can_be_switched_off_and_is_then_recorded_per_article(monkeypatch):
    monkeypatch.setenv("DATAFABRIC_PUBMED_FULLTEXT", "0")
    web = Web(monkeypatch, pmc=jats(CC_BY))
    batch = web.read()
    assert batch["requests"] == 1 and len(batch["records"]) == 2
    assert [c["status"] for c in batch["attachment_coverage"]] == ["fulltext_disabled", "fulltext_disabled"]


def test_license_codes_from_xlink_ali_and_public_domain():
    assert pf._cc_code(["http://creativecommons.org/licenses/by-nc/4.0/"]) == "CC BY-NC 4.0"
    assert pf._cc_code(["https://creativecommons.org/publicdomain/zero/1.0/"]) == "CC0 1.0"
    assert pf._cc_code(["https://example.org/terms"]) is None
    ali = ('<license><ali:license_ref>https://creativecommons.org/licenses/by-sa/3.0/</ali:license_ref>'
           "</license>")
    assert pf.jats_access(jats(ali), "PMC111")["license"] == "CC BY-SA 3.0"


def test_jats_parser_refuses_entity_declarations_and_non_articles(tmp_path):
    bomb = tmp_path / "bomb.xml"
    bomb.write_bytes(b'<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "xx">]><a>&x;</a>')
    with pytest.raises(ValueError):
        jats_text.parse(bomb)
    other = tmp_path / "other.xml"
    other.write_bytes(b"<rss><channel/></rss>")
    assert jats_text.parse(other) is None
    import hashlib

    result = ap.parse_file(store_path=str(other), sha256=hashlib.sha256(other.read_bytes()).hexdigest(),
                           file_ext=".xml")
    assert result["status"] == "unsupported"
