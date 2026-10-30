"""Old imports and persisted Python names resolve to the single implementation."""
import importlib
import pickle

import pytest


MODULES = {
    "scrapling_client": "acquisition.scrapling_client",
    "api_client": "acquisition.api_client",
    "api_router": "acquisition.api_router",
    "pagination_driver": "acquisition.pagination_driver",
    "router": "parsing.router",
    "pdf_meta_sniffer": "parsing.pdf_meta_sniffer",
    "excel_parser": "parsing.excel_parser",
    "docx_parser": "parsing.docx_parser",
    "content_cleaner": "parsing.content_cleaner",
    "transform_pipeline": "parsing.transform_pipeline",
    "quality_checker": "quality.quality_checker",
    "authority_client": "quality.authority_client",
    "task_queue": "queues.task_queue",
    "merge_queue": "queues.merge_queue",
    "lineage_store": "lineage.lineage_store",
    "lineage_tracer": "lineage.lineage_tracer",
    "weknora_graph_client": "sync.weknora_graph_client",
    "weknora_index_client": "sync.weknora_index_client",
    "storage_writer": "storage.storage_writer",
    "cloud_api_client": "common.cloud_api_client",
    "exception_logger": "common.exception_logger",
    "agent_runner": "agents.runner",
    "agent_config": "agents.config",
    "review_exporter": "agents.review_exporter",
}


@pytest.mark.parametrize("old,canonical", MODULES.items())
def test_legacy_and_current_import_share_module_state(old, canonical, monkeypatch):
    legacy = importlib.import_module("core_engine." + old)
    current = importlib.import_module("core_engine." + canonical)
    assert legacy is current
    marker = object()
    monkeypatch.setattr(legacy, "_compatibility_probe", marker, raising=False)
    assert current._compatibility_probe is marker


def test_old_cli_import_preserves_functions_and_pickled_failures():
    import main_etl
    from flows import etl
    from flows import crawl_flow

    assert main_etl is etl
    assert main_etl.fetch_and_parse_url is crawl_flow.fetch_and_parse_url
    assert main_etl.process_and_store is crawl_flow.process_and_store
    assert main_etl.classify_store_result is crawl_flow.classify_store_result
    assert pickle.loads(b"cmain_etl\nCrawlTaskError\n.") is etl.CrawlTaskError
    restored = pickle.loads(pickle.dumps(main_etl.CrawlTaskError("page failed", {"passed": 2})))
    assert restored.stats == {"passed": 2}


def test_old_persisted_queue_class_name_still_resolves():
    from core_engine.queues.task_queue import Task
    assert pickle.loads(b"ccore_engine.task_queue\nTask\n.") is Task


def test_existing_prefect_file_entrypoints_load_the_same_flow_and_task():
    from prefect.flows import load_flow_from_entrypoint
    from prefect.utilities.importtools import import_object
    from core_engine.parsing.router import route_pdf
    from flows import etl

    assert load_flow_from_entrypoint("main_etl.py:main_pipeline") is etl.main_pipeline
    assert import_object("core_engine/router.py:route_pdf") is route_pdf
