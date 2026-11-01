"""Old imports and persisted Python names resolve to the single implementation."""
import pickle



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
    from core_engine.task_queue import Task
    assert pickle.loads(b"ccore_engine.task_queue\nTask\n.") is Task


def test_existing_prefect_file_entrypoints_load_the_same_flow_and_task():
    from prefect.flows import load_flow_from_entrypoint
    from prefect.utilities.importtools import import_object
    from core_engine.router import route_pdf
    from flows import etl

    assert load_flow_from_entrypoint("main_etl.py:main_pipeline") is etl.main_pipeline
    assert import_object("core_engine/router.py:route_pdf") is route_pdf
