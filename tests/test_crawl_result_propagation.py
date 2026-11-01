"""Result propagation and read retries, with all business imports replaced in memory.

No Prefect runtime, database, credentials, HTTP connection or real sleeping is used.
"""
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

import httpx


class _DirectTask:
    def __init__(self, fn, **options):
        self.fn = fn
        self.retries = options.get("retries", 0)

    def __call__(self, *args, **kwargs):
        return self.fn(*args, **kwargs)


def _decorator(**options):
    return lambda fn: _DirectTask(fn, **options)


def _task_mock(**kwargs):
    return _DirectTask(Mock(**kwargs))


def _load_isolated_flow():
    modules = {}
    for package in ("config", "core_engine", "db"):
        module = ModuleType(package)
        module.__path__ = []
        modules[package] = module
    definitions = {
        "prefect": {"task": _decorator, "flow": _decorator},
        "loguru": {"logger": Mock()},
        "config.settings": {"APP": SimpleNamespace(TASK_RETRIES=2, TASK_RETRY_DELAY=0.25)},
        "config.crawler_settings": {"SITE_PROFILES": {"demo": {}}},
        "core_engine.scrapling_client": {
            "fetch_page": _task_mock(return_value={"cookies": []}),
            "download_pdf": _task_mock(), "make_uuid": Mock(), "_sniff_bytes": Mock(),
        },
        "core_engine.router": {"route_webpage": _task_mock(), "route_pdf": _task_mock()},
        "core_engine.docx_parser": {"route_docx": Mock()},
        "core_engine.excel_parser": {"route_excel": Mock()},
        "core_engine.content_cleaner": {"clean_content": _task_mock()},
        "core_engine.quality_checker": {"check_quality": _task_mock(), "quarantine_data": _task_mock()},
        "core_engine.storage_writer": {"write_to_database": _task_mock()},
        "core_engine.transform_pipeline": {
            "apply_l3_normalization": _task_mock(), "apply_identifier_alignment": _task_mock(),
        },
        "db.repository": {"get_document_by_uuid": Mock()},
    }
    for name, attrs in definitions.items():
        module = ModuleType(name)
        vars(module).update(attrs)
        modules[name] = module
    path = Path(__file__).resolve().parents[1] / "flows" / "crawl_flow.py"
    spec = importlib.util.spec_from_file_location("_isolated_crawl_result_flow", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


class CrawlResultPropagationTests(unittest.TestCase):
    def setUp(self):
        self.flow = _load_isolated_flow()
        self.sleep = patch.object(self.flow.time, "sleep").start()
        self.addCleanup(patch.stopall)
        patch("socket.create_connection", side_effect=AssertionError("Network is forbidden in this test")).start()
        self.flow.route_webpage.fn.return_value = self.document()
        self.flow.route_pdf.fn.return_value = self.document("attachment")

    @staticmethod
    def document(uuid="document", markdown="正文资料"):
        return {"uuid": uuid, "markdown": markdown, "title": "", "attachment_links": []}

    def crawl(self, records, outcomes=None):
        with patch.object(self.flow, "fetch_and_parse_url", return_value=records), patch.object(
            self.flow, "process_and_store", side_effect=outcomes,
            return_value={"success": True, "status": "PENDING_REVIEW"},
        ) as store:
            result = self.flow.crawl_url_flow.fn("https://example.invalid/document", "demo")
        return result, store

    def test_only_confirmed_saves_increment_stored(self):
        result, store = self.crawl([self.document("a"), self.document("b")])
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "success")
        self.assertEqual((result["stored"], result["failed"], result["quarantined"]), (2, 0, 0))
        self.assertEqual(store.call_count, 2)

    def test_false_or_unknown_save_results_are_not_success(self):
        for outcome in [False, None, {}, {"success": False, "status": "failed"}, {"success": 1}]:
            with self.subTest(outcome=outcome):
                result, store = self.crawl([self.document()], [outcome])
                self.assertFalse(result["success"])
                self.assertEqual((result["stored"], result["failed"]), (0, 1))
                self.assertEqual(result["status"], "failed")
                self.assertEqual(store.call_count, 1)
        self.sleep.assert_not_called()

    def test_unknown_save_exception_is_never_retried(self):
        result, store = self.crawl([self.document()], [httpx.ReadTimeout("commit acknowledgement unknown")])
        self.assertFalse(result["success"])
        self.assertEqual((result["stored"], result["failed"]), (0, 1))
        self.assertEqual(store.call_count, 1)
        self.sleep.assert_not_called()

    def test_mixed_results_remain_partial_and_failed(self):
        records = [self.document("a"), self.document("b"), self.document("c")]
        result, store = self.crawl(records, [
            {"success": True}, {"success": False, "error": "not committed"},
            {"success": False, "status": "quarantined"},
        ])
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual((result["total"], result["stored"], result["failed"], result["quarantined"]), (3, 1, 1, 1))
        self.assertEqual(store.call_count, 3)
        self.assertEqual([item["uuid"] for item in result["failures"]], ["b", "c"])

    def test_quarantined_is_not_stored_or_a_storage_failure(self):
        result, store = self.crawl([self.document()], [{"success": False, "status": "quarantined"}])
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual((result["stored"], result["failed"], result["quarantined"]), (0, 0, 1))
        self.assertEqual(store.call_count, 1)

    def test_explicit_empty_result_is_explained_and_does_not_write(self):
        result, store = self.crawl([])
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "empty")
        self.assertTrue(result["message"])
        self.assertEqual(result["total"], 0)
        store.assert_not_called()

    def test_fetch_exception_is_not_empty_success(self):
        with patch.object(self.flow, "fetch_and_parse_url", side_effect=TimeoutError("fetch failed")), patch.object(
            self.flow, "process_and_store",
        ) as store:
            result = self.flow.crawl_url_flow.fn("https://example.invalid", "demo")
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_stage"], "fetch_parse")
        store.assert_not_called()

    def test_invalid_fetch_return_is_not_a_legal_zero_match(self):
        for records in [None, {}, ""]:
            with self.subTest(records=records):
                result, store = self.crawl(records)
                self.assertFalse(result["success"])
                self.assertEqual(result["error_stage"], "fetch_parse")
                store.assert_not_called()

    def test_body_without_attachments_is_valid(self):
        records = self.flow.fetch_and_parse_url.fn("https://example.invalid", "demo", {})
        self.assertEqual(records, [self.document()])
        self.flow.download_pdf.fn.assert_not_called()

    def test_empty_or_invalid_web_parser_result_raises(self):
        for parsed in [None, {}, self.document(markdown=""), self.document(markdown="   "),
                       {**self.document(markdown="# 标题"), "title": "标题"},
                       {**self.document(), "success": False, "error": "parser failed"}]:
            with self.subTest(parsed=parsed):
                self.flow.route_webpage.fn.return_value = parsed
                with self.assertRaises(ValueError):
                    self.flow.fetch_and_parse_url.fn("https://example.invalid", "demo", {})
        self.sleep.assert_not_called()

    def test_attachment_only_page_with_valid_content_is_not_rejected(self):
        parent = self.document(markdown="")
        parent["attachment_links"] = [{"url": "https://example.invalid/file.pdf"}]
        self.flow.route_webpage.fn.return_value = parent
        self.flow.download_pdf.fn.return_value = {"file_ext": ".pdf", "local_path": "/not-opened.pdf"}
        records = self.flow.fetch_and_parse_url.fn("https://example.invalid", "demo", {})
        self.assertEqual(records, [self.document("attachment"), parent])

    def test_attachment_parser_empty_output_is_not_success(self):
        self.flow.download_pdf.fn.return_value = {"file_ext": ".pdf", "local_path": "/not-opened.pdf"}
        self.flow.route_pdf.fn.return_value = self.document("attachment", "")
        with self.assertRaises(ValueError):
            self.flow.fetch_and_parse_attachments.fn([{"url": "https://example.invalid/file.pdf"}], "parent", "demo")
        self.assertEqual(self.flow.download_pdf.fn.call_count, 1)
        self.sleep.assert_not_called()

    def test_attachment_only_page_with_no_parsable_attachment_is_failure(self):
        parent = self.document(markdown="")
        parent["attachment_links"] = [{"url": "https://example.invalid/file.unknown"}]
        self.flow.route_webpage.fn.return_value = parent
        self.flow.download_pdf.fn.return_value = {"file_ext": ".unknown", "local_path": ""}
        with self.assertRaisesRegex(ValueError, "没有可解析附件"):
            self.flow.fetch_and_parse_url.fn("https://example.invalid", "demo", {})

    def test_real_httpx_timeout_is_retried_in_fn_read_path(self):
        self.flow.fetch_page.fn.side_effect = [httpx.ReadTimeout("read timed out"), {"cookies": []}]
        records = self.flow.fetch_and_parse_url.fn("https://example.invalid", "demo", {})
        self.assertEqual(records, [self.document()])
        self.assertEqual(self.flow.fetch_page.fn.call_count, 2)
        self.assertEqual(self.sleep.call_args_list, [call(0.25)])
        self.assertEqual(self.flow.route_webpage.fn.call_count, 1)

    def test_existing_runtime_http_429_message_is_retried_with_backoff(self):
        self.flow.fetch_page.fn.side_effect = [RuntimeError("HTTP 429，页面请求被拒绝"),
                                               RuntimeError("HTTP 429，页面请求被拒绝"), {"cookies": []}]
        self.flow.fetch_and_parse_url.fn("https://example.invalid", "demo", {})
        self.assertEqual(self.flow.fetch_page.fn.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [call(0.25), call(0.5)])

    def test_real_httpx_429_exception_is_retried(self):
        request = httpx.Request("GET", "https://example.invalid")
        error = httpx.HTTPStatusError("throttled", request=request, response=httpx.Response(429, request=request))
        read = Mock(side_effect=[error, "retrieved"])
        self.assertEqual(self.flow._read_with_retries(read), "retrieved")
        self.assertEqual(read.call_count, 2)
        self.sleep.assert_called_once_with(0.25)

    def test_other_http_errors_do_not_retry(self):
        request = httpx.Request("GET", "https://example.invalid")
        errors = [RuntimeError("HTTP 403，页面请求被拒绝"), RuntimeError("HTTP 500，页面请求被拒绝"),
                  httpx.HTTPStatusError("not found", request=request, response=httpx.Response(404, request=request))]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.sleep.reset_mock()
                read = Mock(side_effect=error)
                with self.assertRaises(type(error)):
                    self.flow._read_with_retries(read)
                self.assertEqual(read.call_count, 1)
                self.sleep.assert_not_called()

    def test_transient_read_retries_are_bounded(self):
        self.flow.fetch_page.fn.side_effect = httpx.ReadTimeout("still timed out")
        with self.assertRaises(httpx.ReadTimeout):
            self.flow.fetch_and_parse_url.fn("https://example.invalid", "demo", {})
        self.assertEqual(self.flow.fetch_page.fn.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [call(0.25), call(0.5)])
        self.flow.route_webpage.fn.assert_not_called()

    def test_backoff_delay_is_capped(self):
        self.flow.APP.TASK_RETRY_DELAY = 40
        read = Mock(side_effect=[TimeoutError(), TimeoutError(), "retrieved"])
        self.assertEqual(self.flow._read_with_retries(read), "retrieved")
        self.assertEqual(self.sleep.call_args_list, [call(40.0), call(60.0)])

    def test_attachment_download_uses_same_bounded_read_policy(self):
        self.flow.download_pdf.fn.side_effect = [httpx.ReadTimeout("attachment timed out"),
                                                {"file_ext": ".pdf", "local_path": "/not-opened.pdf"}]
        records = self.flow.fetch_and_parse_attachments.fn([{"url": "https://example.invalid/file.pdf"}], "parent", "demo")
        self.assertEqual(records, [self.document("attachment")])
        self.assertEqual(self.flow.download_pdf.fn.call_count, 2)
        self.sleep.assert_called_once_with(0.25)
        self.assertEqual(self.flow.route_pdf.fn.call_count, 1)

    def test_parser_timeout_is_not_a_fetch_retry(self):
        self.flow.route_webpage.fn.side_effect = httpx.ReadTimeout("parser failed")
        with self.assertRaises(httpx.ReadTimeout):
            self.flow.fetch_and_parse_url.fn("https://example.invalid", "demo", {})
        self.assertEqual(self.flow.fetch_page.fn.call_count, 1)
        self.sleep.assert_not_called()

    def test_task_and_write_flow_do_not_add_automatic_retries(self):
        self.assertEqual(self.flow.fetch_and_parse_url.retries, 0)
        self.assertEqual(self.flow.fetch_and_parse_attachments.retries, 0)
        self.assertEqual(self.flow.process_and_store.retries, 0)
        self.assertEqual(self.flow.crawl_url_flow.retries, 0)


if __name__ == "__main__":
    unittest.main()
