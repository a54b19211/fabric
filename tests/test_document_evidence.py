"""文档交接保留全文、来源关系和未知状态；不把声明当核实。"""
import json

import pytest

from fetch.projection import project_record
from store.history_models import ArchiveRecord


def archive(**extra):
    data = {"record_kind": "document", "title": "更正", "summary": "短摘要", **extra}
    return ArchiveRecord(source_record_id="correction-1", record_kind="document",
                         raw_content=json.dumps(data, ensure_ascii=False))


def test_full_text_and_correction_are_available_without_inventing_target_version():
    result = project_record("external_archive", archive(
        full_text="公告前半段。\n原40套更正为32套，金额6400万元。",
        material_kind="corporate_disclosure",
        document_relations=[{"relation_type": "corrects", "target_source_id": "external_archive",
                             "target_source_record_id": "original-1"}],
    ))
    assert result["clean_text"].endswith("原40套更正为32套，金额6400万元。")
    evidence = result["document_evidence"]
    assert evidence["material_kind"] == "corporate_disclosure"
    assert evidence["text_level"] == "full_text"
    relation = evidence["document_relations"][0]
    assert relation["target_source_record_id"] == "original-1"
    assert relation["target_version_id"] is None
    assert relation["basis_locator"]["json_pointer"] == "/document_relations/0"
    assert result["field_locators"]["full_text"] == {"json_pointer": "/full_text"}


def test_legacy_summary_is_not_claimed_as_full_text_or_primary_research():
    result = project_record("external_archive", archive())
    assert result["document_evidence"]["material_kind"] is None
    assert result["document_evidence"]["text_level"] == "abstract"
    assert result["document_evidence"]["source_independence"]["status"] == "unknown"


def test_social_report_remains_unverified_even_with_a_publication_date():
    result = project_record("external_archive", archive(
        full_text="据称有人就医，尚待核实。", material_kind="social_media",
        source_published_at="2020-01-01"))
    assert result["document_evidence"]["statement_status"] == "unverified_report"
    assert result["source_published_at"]["meaning"] == "archive_source_published"
    assert "availability_proof" not in result


def test_europe_pmc_records_same_paper_identity_not_independent_research():
    result = project_record("europe_pmc", ArchiveRecord(
        source_record_id="MED:123", record_kind="document",
        raw_content=json.dumps({"source": "MED", "id": "123", "pmid": "123", "doi": "10.1234/example",
                                "title": "Title", "abstractText": "Abstract",
                                "pubTypeList": {"pubType": ["Journal Article"]}})))
    evidence = result["document_evidence"]
    assert evidence["material_kind"] is None
    assert evidence["text_level"] == "abstract"
    assert evidence["source_independence"]["origin_group_id"] == "doi:10.1234/example"
    assert evidence["source_independence"]["status"] == "same_origin"


def test_pubmed_correction_direction_and_native_xml_locator():
    raw = """<PubmedArticle><MedlineCitation><PMID>124</PMID><Article>
    <ArticleTitle>Correction</ArticleTitle><PublicationTypeList>
    <PublicationType>Published Erratum</PublicationType></PublicationTypeList></Article>
    <CommentsCorrectionsList><CommentsCorrections RefType="ErratumFor">
    <RefSource>Original</RefSource><PMID>123</PMID></CommentsCorrections>
    <CommentsCorrections RefType="ErratumIn"><RefSource>Later</RefSource><PMID>125</PMID>
    </CommentsCorrections></CommentsCorrectionsList></MedlineCitation></PubmedArticle>"""
    result = project_record("pubmed", ArchiveRecord(source_record_id="124", record_kind="document",
                            raw_media_type="application/xml", raw_content=raw))
    relations = result["document_evidence"]["document_relations"]
    assert len(relations) == 1
    assert relations[0]["relation_type"] == "corrects"
    assert relations[0]["target_source_record_id"] == "123"
    assert relations[0]["basis_locator"]["xpath"].endswith("CommentsCorrections[1]")


@pytest.mark.parametrize("source, raw", [
    ("who_don", {"Id": "00000000-0000-0000-0000-000000000001", "Title": "Notice"}),
    ("fda_crl", {"file_name": "example.pdf", "letter_date": "01/01/2020",
                 "letter_type": "CRL", "application_number": ["NDA123456"]}),
])
def test_official_identity_does_not_prove_body_is_present(source, raw):
    from fetch.projection import crl_id
    native = raw["Id"] if source == "who_don" else crl_id(raw)
    result = project_record(source, ArchiveRecord(source_record_id=native, record_kind="document",
                            raw_content=json.dumps(raw)))
    assert result["document_evidence"]["text_level"] in {"title", "metadata"}


def test_partial_bibliographic_ids_still_have_shared_origin_constraint():
    common = {"source": "MED", "id": "123", "pmid": "123", "title": "Title"}
    evidence = []
    for data in [dict(common, doi="10.1234/example"), common]:
        result = project_record("europe_pmc", ArchiveRecord(
            source_record_id="MED:123", record_kind="document", raw_content=json.dumps(data)))
        evidence.append(result["document_evidence"]["source_independence"])
    assert set(evidence[0]["shared_identifiers"]) & set(evidence[1]["shared_identifiers"]) == {"pmid:123"}
    # 一侧只有DOI也能重合；单一origin_group_id不能承担完整同源判断。
    only_doi = {"source": "PMC", "id": "PMC123", "title": "Title", "doi": "10.1234/example"}
    result = project_record("europe_pmc", ArchiveRecord(
        source_record_id="PMC:PMC123", record_kind="document", raw_content=json.dumps(only_doi)))
    assert "doi:10.1234/example" in result["document_evidence"]["source_independence"]["shared_identifiers"]
