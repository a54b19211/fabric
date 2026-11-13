"""Which contract, endpoint and checker each producer's products are checked under (clean.extraction_checks)."""

import extraction_contract as ec

from clean import extraction_checks as checks


def test_identifiers_and_annotations_go_to_v1_and_measurements_to_v3():
    table = {p: (c.contract, c.path, c.checker) for p, c in checks.CHECKS_BY_PRODUCER.items()}
    assert table == {
        "identifiers": ("extraction_consistency.v1", "/v1/extraction-consistency", "biocheck.extraction_rules.v3"),
        "profile": ("extraction_consistency.v1", "/v1/extraction-consistency", "biocheck.extraction_rules.v3"),
        "measures": ("extraction_consistency.v3", "/v3/extraction-consistency", "biocheck.extraction_rules.v17"),
    }
    # every default checker answers under the contract its producer is recorded with (the shared table)
    for check in checks.CHECKS_BY_PRODUCER.values():
        assert ec.CONTRACT_OF[check.checker] == check.contract


def test_each_producer_expects_its_own_checker_and_nothing_in_the_environment_changes_it(monkeypatch):
    monkeypatch.setenv("DATAFABRIC_EXTRACTION_CHECKER", "biocheck.extraction_rules.v4")
    monkeypatch.setenv("DATAFABRIC_MEASURE_CHECKER", "biocheck.extraction_rules.v4")
    assert checks.expected_checker() == checks.expected_checker("identifiers") == "biocheck.extraction_rules.v3"
    assert checks.expected_checker("profile") == "biocheck.extraction_rules.v3"
    assert checks.expected_checker("measures") == "biocheck.extraction_rules.v17"


def test_the_existing_producers_keep_their_receipt_constants():
    assert checks.PRODUCERS == ("identifiers", "profile")
    assert checks.CONTRACT == "extraction_consistency.v1"
