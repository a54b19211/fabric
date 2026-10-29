"""真实 main_etl 定义的离线回归：不导入 Prefect、数据库或网络客户端。"""
import ast
import pickle
import sys
import types
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import Mock, patch


SOURCE = Path(__file__).resolve().parents[1] / "main_etl.py"
# 本文件单独在临时目录复核时，也读取同目录的真实源文件副本。
if not SOURCE.is_file():
    SOURCE = Path(__file__).with_name("main_etl.py")
NAMES = {
    "CrawlTaskError", "_new_stats", "_merge_stats", "_crawl_status", "_update_task_log",
    "_process_single_result", "crawl_detail_page", "crawl_single_start_url", "crawl_websites_flow",
    "process_local_pdfs_flow", "main_pipeline",
}


class FakeTask:
    def __init__(self, fn):
        self.fn = fn

    def submit(self, *args, **kwargs):
        def result():
            return self.fn(*args, **kwargs)
        return types.SimpleNamespace(result=result)


class MainEtlResultsTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                 and node.name in NAMES]
        for node in nodes:
            if isinstance(node, ast.FunctionDef):
                node.decorator_list = []
        module = types.ModuleType("isolated_main_etl_results")
        self.ns = module.__dict__
        self.ns.update(logger=Mock(), Optional=Optional, SITE_PROFILES={},
                       APP=types.SimpleNamespace(DB_HOST=""), LOCAL_PDF_SCAN_DIRS=[],
                       _ensure_dirs=Mock(), check_connection=Mock(), run_migrations=Mock(),
                       export_review_csv=Mock(return_value=None), scan_local_pdfs=Mock(),
                       _validate_parsed_document=Mock(side_effect=lambda value: value))
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), self.ns)
        self.ns["crawl_detail_page"] = FakeTask(self.ns["crawl_detail_page"])
        self.ns["crawl_single_start_url"] = FakeTask(self.ns["crawl_single_start_url"])
        self.fetch = Mock(side_effect=lambda url, *a, **k: [{"uuid": url}])
        self.store = Mock(return_value={"success": True})
        self.ns.update(fetch_and_parse_url=types.SimpleNamespace(fn=self.fetch),
                       process_and_store=types.SimpleNamespace(fn=self.store))
        self.old = Mock(return_value=True)
        self.ns["_already_processed"] = self.old
        repository = types.ModuleType("db.repository")
        repository.log_crawl_task = Mock(return_value=1)
        repository.update_crawl_log = Mock()
        self.log = repository.update_crawl_log
        pagination = types.ModuleType("core_engine.pagination_driver")
        pagination.JsClickDriver = type("FakeJsDriver", (), {})
        self.pagination = pagination
        self.modules = patch.dict(sys.modules, {
            "isolated_main_etl_results": module,
            "db.repository": repository,
            "core_engine.pagination_driver": pagination,
        })
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def driver(self, urls):
        def iterate():
            for value in urls:
                if isinstance(value, Exception):
                    raise value
                yield value
        self.ns["make_driver"] = Mock(return_value=types.SimpleNamespace(iter_detail_urls=iterate))

    def crawl(self, urls, mode="incremental"):
        self.driver(urls)
        return self.ns["crawl_single_start_url"].fn("site", {}, "list", mode)

    def test_storage_false_is_failed_not_quarantined_or_passed(self):
        self.store.return_value = {"success": False}
        result = self.crawl(["one"])
        self.assertEqual(result["stats"], {"total": 1, "passed": 0, "quarantined": 0, "failed": 1})
        self.assertEqual(self.log.call_args.kwargs["status"], "failed")
        self.assertEqual(self.log.call_args.kwargs["failed"], 1)

    def test_missing_save_confirmation_is_not_success(self):
        self.store.return_value = {}
        self.assertEqual(self.crawl(["one"])["stats"]["failed"], 1)

    def test_save_exception_keeps_later_records_and_partial_log(self):
        self.store.side_effect = [RuntimeError("commit unknown"), {"success": True}]
        result = self.crawl(["one", "two"])
        self.assertEqual(result["stats"], {"total": 2, "passed": 1, "quarantined": 0, "failed": 1})
        self.assertEqual(self.store.call_count, 2)
        self.assertEqual(self.log.call_args.kwargs["status"], "partial")
        self.assertIn("commit unknown", self.log.call_args.kwargs["error"])

    def test_quality_quarantine_remains_distinct(self):
        self.store.return_value = {"success": False, "status": "quarantined"}
        stats = self.crawl(["one"])["stats"]
        self.assertEqual(stats, {"total": 1, "passed": 0, "quarantined": 1, "failed": 0})

    def test_fetch_failure_is_not_zero_hits_and_later_url_runs(self):
        self.fetch.side_effect = [TimeoutError("read timed out"), [{"uuid": "two"}]]
        stats = self.crawl(["one", "two"])["stats"]
        self.assertEqual(stats, {"total": 2, "passed": 1, "quarantined": 0, "failed": 1})
        self.assertEqual(self.store.call_count, 1)

    def test_legitimate_empty_fetch_is_success_with_zero_records(self):
        self.fetch.return_value = []
        self.fetch.side_effect = None
        result = self.crawl(["empty"])
        self.assertEqual(result["stats"], self.ns["_new_stats"]())
        self.assertEqual(result["status"], "success")
        self.store.assert_not_called()
        self.assertEqual(self.log.call_args.kwargs["status"], "success")

    def test_invalid_fetch_contract_is_failure_not_zero_hits(self):
        self.fetch.side_effect = None
        self.fetch.return_value = None
        self.assertEqual(self.crawl(["invalid"])["stats"]["failed"], 1)

    def test_old_then_new_out_of_order_and_repeated_urls_do_not_stop(self):
        result = self.crawl(["old", "new1", "old", "new2", "new1"])
        self.assertEqual([c.args[0] for c in self.fetch.call_args_list], ["old", "new1", "new2"])
        self.assertEqual(result["stats"]["passed"], 3)
        self.old.assert_not_called()

    def test_full_mode_also_reads_old_id_and_submits_revision_to_storage(self):
        self.fetch.side_effect = None
        self.fetch.return_value = [{"uuid": "old", "markdown": "revised content"}]
        result = self.crawl(["old"], mode="full")
        self.store.assert_called_once_with({"uuid": "old", "markdown": "revised content"})
        self.assertEqual(result["stats"]["passed"], 1)
        self.old.assert_not_called()

    def test_js_batches_keep_later_new_urls_and_deduplicate(self):
        driver = self.pagination.JsClickDriver()
        driver._playwright_crawl = lambda: [["old", "old"], ["new", "old"]]
        self.ns["make_driver"] = Mock(return_value=driver)
        result = self.ns["crawl_single_start_url"].fn("site", {}, "list", "incremental")
        self.assertEqual([c.args[0] for c in self.fetch.call_args_list], ["old", "new"])
        self.assertEqual(result["stats"]["passed"], 2)

    def test_second_page_failure_keeps_completed_stats_and_does_not_retry_writes(self):
        self.driver(["old", TimeoutError("page two failed")])
        with self.assertRaises(self.ns["CrawlTaskError"]) as caught:
            self.ns["crawl_single_start_url"].fn("site", {}, "list")
        self.assertEqual(caught.exception.stats["passed"], 1)
        self.assertIn("page two failed", str(caught.exception))
        self.store.assert_called_once()
        self.assertEqual(self.log.call_args.kwargs["status"], "partial")
        self.assertIn("page two failed", self.log.call_args.kwargs["error"])
        restored = pickle.loads(pickle.dumps(caught.exception))
        self.assertEqual(restored.stats, caught.exception.stats)

    def test_first_page_failure_is_failed_not_legitimate_empty(self):
        self.driver([TimeoutError("page one failed")])
        with self.assertRaises(self.ns["CrawlTaskError"]):
            self.ns["crawl_single_start_url"].fn("site", {}, "list")
        self.assertEqual(self.log.call_args.kwargs["status"], "failed")
        self.store.assert_not_called()

    def test_explicit_replay_after_interruption_revisits_old_and_reaches_new(self):
        self.driver(["old", TimeoutError("interrupted")])
        with self.assertRaises(self.ns["CrawlTaskError"]):
            self.ns["crawl_single_start_url"].fn("site", {}, "list")
        result = self.crawl(["old", "old", "new"])
        self.assertEqual([c.args[0] for c in self.fetch.call_args_list], ["old", "old", "new"])
        self.assertEqual(result["stats"]["passed"], 2)
        self.assertEqual(self.log.call_args.kwargs["status"], "success")

    def test_unknown_save_outcome_not_retried_for_duplicate_in_same_run(self):
        self.store.side_effect = RuntimeError("commit outcome unknown")
        result = self.crawl(["one", "one", "one"])
        self.store.assert_called_once()
        self.assertEqual(result["stats"]["failed"], 1)
        tree = ast.parse(SOURCE.read_text())
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in ("crawl_detail_page", "crawl_single_start_url"):
                retries = [k.value for d in node.decorator_list if isinstance(d, ast.Call)
                           for k in d.keywords if k.arg == "retries"]
                self.assertEqual(len(retries), 1)
                self.assertEqual(ast.literal_eval(retries[0]), 0)

    def test_web_flow_retains_partial_stats_from_failed_entry(self):
        self.ns["SITE_PROFILES"] = {"site": {"name": "Site", "start_urls": ["list"]}}
        self.driver(["one", TimeoutError("later page failed")])
        result = self.ns["crawl_websites_flow"]()
        self.assertEqual(result["stats"]["passed"], 1)
        self.assertEqual(result["failed_sites"], {"site": ["list"]})
        self.assertEqual(result["status"], "partial")

    def test_api_second_page_failure_is_partial_not_exhausted_success(self):
        self.ns["SITE_PROFILES"] = {"api": {"name": "API", "source_type": "api"}}
        def records(*args):
            yield {"uuid": "one"}
            raise TimeoutError("api page two failed")
        self.ns["iter_api_results"] = records
        result = self.ns["crawl_websites_flow"]()
        self.assertEqual(result["stats"]["passed"], 1)
        self.assertEqual(result["failed_sites"], {"api": ["api"]})
        self.assertEqual(self.log.call_args.kwargs["status"], "partial")
        self.assertIn("api page two failed", self.log.call_args.kwargs["error"])

    def test_api_zero_hits_and_save_failure_have_distinct_status(self):
        self.ns["SITE_PROFILES"] = {"api": {"name": "API", "source_type": "api"}}
        self.ns["iter_api_results"] = lambda *args: iter(())
        empty = self.ns["crawl_websites_flow"]()
        self.assertEqual(empty["status"], "success")
        self.assertEqual(empty["stats"], self.ns["_new_stats"]())
        self.ns["iter_api_results"] = lambda *args: iter([{"uuid": "one"}])
        self.store.return_value = {"success": False}
        failed = self.ns["crawl_websites_flow"]()
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["stats"]["failed"], 1)
        self.assertEqual(self.log.call_args.kwargs["failed"], 1)

    def test_table_save_failure_is_not_quarantine(self):
        self.ns["make_driver"] = lambda *a: types.SimpleNamespace(iter_table_data=lambda: iter([[{"uuid": "row"}]]))
        self.store.return_value = {"success": False}
        result = self.ns["crawl_single_start_url"].fn("table", {"source_type": "table_page"}, "list")
        self.assertEqual(result["stats"]["failed"], 1)
        self.assertEqual(result["stats"]["quarantined"], 0)


    def test_local_pdf_parser_failure_is_counted_and_returned(self):
        self.ns["LOCAL_PDF_SCAN_DIRS"] = ["offline-dir"]
        self.old.return_value = False
        self.ns["scan_local_pdfs"].return_value = [
            {"uuid": "pdf", "source_site": "local", "local_path": "offline.pdf"}]
        self.ns["route_pdf"] = types.SimpleNamespace(fn=Mock(side_effect=RuntimeError("parser failed")))
        result = self.ns["process_local_pdfs_flow"]()
        self.assertEqual(result["stats"], {"total": 1, "passed": 0, "quarantined": 0, "failed": 1})
        self.assertEqual(result["status"], "failed")
        self.assertIn("parser failed", result["error"])
        self.store.assert_not_called()

    def test_local_pdf_empty_parser_result_does_not_become_successful_quarantine(self):
        self.ns["LOCAL_PDF_SCAN_DIRS"] = ["offline-dir"]
        self.old.return_value = False
        self.ns["scan_local_pdfs"].return_value = [
            {"uuid": "pdf", "source_site": "local", "local_path": "offline.pdf"}]
        empty = {"uuid": "pdf", "markdown": "", "title": "title"}
        self.ns["route_pdf"] = types.SimpleNamespace(fn=Mock(return_value=empty))
        # Shared validator semantics are tested in test_crawl_result_propagation;
        # here assert the local path uses it before any quality/storage call.
        validator = Mock(side_effect=ValueError("文档正文解析为空"))
        self.ns["_validate_parsed_document"] = validator
        self.store.return_value = {"success": False, "status": "quarantined"}
        result = self.ns["process_local_pdfs_flow"]()
        validator.assert_called_once_with(empty)
        self.store.assert_not_called()
        self.assertEqual(result["stats"], {"total": 1, "passed": 0, "quarantined": 0, "failed": 1})
        self.assertEqual(result["status"], "failed")

    def test_root_raises_with_partial_crawl_stats_instead_of_successful_exit(self):
        stats = {"total": 2, "passed": 1, "quarantined": 0, "failed": 1}
        self.ns["crawl_websites_flow"] = Mock(return_value={
            "stats": stats, "status": "partial", "failed_sites": {"site": ["list"]}})
        with self.assertRaises(self.ns["CrawlTaskError"]) as caught:
            self.ns["main_pipeline"](mode="all")
        self.assertEqual(caught.exception.stats, stats)
        self.ns["export_review_csv"].assert_not_called()
        self.ns["check_connection"].assert_not_called()
        self.ns["run_migrations"].assert_not_called()

    def test_root_combines_successful_crawl_with_failed_local_stats(self):
        self.ns["LOCAL_PDF_SCAN_DIRS"] = ["offline-dir"]
        self.ns["crawl_websites_flow"] = Mock(return_value={
            "stats": {"total": 2, "passed": 2, "quarantined": 0, "failed": 0}, "status": "success"})
        self.ns["process_local_pdfs_flow"] = Mock(return_value={
            "stats": {"total": 1, "passed": 0, "quarantined": 0, "failed": 1},
            "status": "failed", "error": "save failed"})
        with self.assertRaises(self.ns["CrawlTaskError"]) as caught:
            self.ns["main_pipeline"](mode="all")
        self.assertEqual(caught.exception.stats, {"total": 3, "passed": 2, "quarantined": 0, "failed": 1})
        self.assertIn("save failed", str(caught.exception))
        self.ns["export_review_csv"].assert_not_called()

    def test_root_success_returns_completed_stats(self):
        stats = {"total": 1, "passed": 1, "quarantined": 0, "failed": 0}
        self.ns["crawl_websites_flow"] = Mock(return_value={"stats": stats, "status": "success"})
        result = self.ns["main_pipeline"](mode="crawl")
        self.assertEqual(result, {"stats": stats, "status": "success"})
        self.ns["_ensure_dirs"].assert_called_once()

    def test_all_write_flows_disable_automatic_batch_retries(self):
        expected = {"crawl_websites_flow", "process_local_pdfs_flow", "main_pipeline"}
        tree = ast.parse(SOURCE.read_text())
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in expected:
                values = [k.value for d in node.decorator_list if isinstance(d, ast.Call)
                          for k in d.keywords if k.arg == "retries"]
                self.assertEqual(len(values), 1, node.name)
                self.assertEqual(ast.literal_eval(values[0]), 0, node.name)
                expected.remove(node.name)
        self.assertFalse(expected)


if __name__ == "__main__":
    unittest.main()
