"""Streaming and persisted-report integration tests with isolated SQLite/CSV data."""

import csv
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from checkmodel.base import RiskAbstention
from markupsafe import escape
from werkzeug.datastructures import MultiDict
from webapp import create_app, reports

import test_detect_routes as detect_test_helpers

RISK_IDS = detect_test_helpers.RISK_IDS


class HistoryStreamTests(unittest.TestCase):
    # Reuse isolation and model stubs without inheriting unrelated test methods.
    setUp = detect_test_helpers.DetectRouteTests.setUp
    capture_template = detect_test_helpers.DetectRouteTests.capture_template
    lookup_sources = detect_test_helpers.DetectRouteTests.lookup_sources
    lookup_model = detect_test_helpers.DetectRouteTests.lookup_model
    database_snapshot = detect_test_helpers.DetectRouteTests.database_snapshot
    configure_scores = detect_test_helpers.DetectRouteTests.configure_scores
    submit = detect_test_helpers.DetectRouteTests.submit

    def form(self, message="待检测的中文消息。", models=None, mode="vote", deepseek_source=None):
        values = MultiDict([("message", message), ("mode", mode)])
        if deepseek_source is not None:
            values.add("deepseek_source", deepseek_source)
        for model_id in RISK_IDS if models is None else models:
            values.add("models", model_id)
        return values

    def events(self, response):
        pending = b""
        for chunk in response.response:
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                if line.strip():
                    yield json.loads(line)
        if pending.strip():
            yield json.loads(pending)

    def complete_stream(self, **kwargs):
        response = self.client.post("/detect/stream", data=self.form(**kwargs), buffered=False)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/x-ndjson")
        try:
            events = list(self.events(response))
        finally:
            response.close()
        self.assertEqual(events[-1]["type"], "complete")
        return events

    def json_report(self, record_id):
        response = self.client.get("/history/{}/export?format=json".format(record_id))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/json")
        return response.get_json()

    def test_stream_announces_each_member_before_calling_it_then_saves_once(self):
        self.configure_scores([10, 50, 90])
        response = self.client.post("/detect/stream", data=self.form(), buffered=False)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/x-ndjson")
        received = self.events(response)
        completed_members = []
        try:
            for index, model_id in enumerate(RISK_IDS):
                started = next(received)
                self.assertEqual(started["type"], "member_start")
                self.assertEqual(started["member"]["id"], model_id)
                self.assertEqual(started["member"]["status"], "running")
                for remaining_id in RISK_IDS[index:]:
                    self.model_instances[remaining_id].check.assert_not_called()
                finished = next(received)
                self.assertEqual(finished["type"], "member_complete")
                self.assertEqual(finished["member"]["id"], model_id)
                self.assertEqual(finished["member"]["status"], "ok")
                self.assertEqual(finished["member"]["ui_display_name"], "分析模型 {}".format(index + 1))
                self.model_instances[model_id].check.assert_called_once()
                completed_members.append(finished["member"])
            complete = next(received)
            self.assertEqual(complete["type"], "complete")
            self.assertTrue(complete["record_id"])
            self.assertEqual(complete["history_url"], "/history/{}".format(complete["record_id"]))
            self.assertIsNone(complete["history_error"])
            self.assertIn("data-risk-result", complete["html"])
            with self.assertRaises(StopIteration):
                next(received)
        finally:
            response.close()
        stored = self.json_report(complete["record_id"])
        # UI labels are transport-only; every analysis field must match storage.
        self.assertEqual(stored["result"]["members"], [
            {key: value for key, value in member.items() if key != "ui_display_name"}
            for member in completed_members
        ])
        self.assertEqual(stored["result"]["decision_method"], "mean_fallback")
        self.assertEqual(stored["result"]["mean_score"], 50)
        self.assertEqual(reports.list_reports()["total"], 1)
        self.assertEqual(self.database_snapshot(), self.initial_database)

    def test_member_exception_and_abstention_do_not_stop_stream_or_enter_mean(self):
        self.model_instances[RISK_IDS[0]].check.return_value = (91, "中文风险理由")
        self.model_instances[RISK_IDS[1]].check.side_effect = RuntimeError("PRIVATE_INTERNAL_DETAIL")
        self.model_instances[RISK_IDS[2]].check.side_effect = RiskAbstention("上下文不足")
        events = self.complete_stream()
        members = [event["member"] for event in events if event["type"] == "member_complete"]
        self.assertEqual([member["status"] for member in members], ["ok", "error", "abstained"])
        self.assertNotIn("PRIVATE_INTERNAL_DETAIL", json.dumps(events, ensure_ascii=False))
        result = self.json_report(events[-1]["record_id"])["result"]
        self.assertEqual(result["decision_method"], "mean_fallback")
        self.assertEqual(result["mean_score"], 91)
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["selected_count"], 3)

    def test_invalid_stream_request_has_no_model_calls_or_history(self):
        invalid_forms = [
            self.form(message=""),
            self.form(models=[RISK_IDS[0], RISK_IDS[0]]),
            self.form(models=[RISK_IDS[0], "roberta_rumor"]),
        ]
        for form in invalid_forms:
            with self.subTest(form=form):
                response = self.client.post("/detect/stream", data=form)
                self.assertEqual(response.status_code, 400)
                payload = response.get_json()
                self.assertEqual(payload["type"], "error")
                self.assertTrue(payload["message"])
                self.get_model.assert_not_called()
                self.assertEqual(reports.list_reports()["total"], 0)

    def test_disconnecting_after_one_completed_member_does_not_save_report(self):
        response = self.client.post("/detect/stream", data=self.form(), buffered=False)
        iterator = self.events(response)
        self.assertEqual(next(iterator)["type"], "member_start")
        self.assertEqual(next(iterator)["type"], "member_complete")
        response.close()
        iterator.close()
        self.model_instances[RISK_IDS[0]].check.assert_called_once()
        for model_id in RISK_IDS[1:]:
            self.model_instances[model_id].check.assert_not_called()
        self.assertEqual(reports.list_reports()["total"], 0)

    def test_both_sync_paths_save_one_complete_report_without_changing_csv(self):
        for count, endpoint in enumerate(("/detect", "/detect/check"), 1):
            response, context = self.submit(endpoint, message="同步路径 {}".format(count))
            listing = reports.list_reports()
            self.assertEqual(listing["total"], count)
            report = reports.get_report(listing["records"][0]["id"])
            self.assertEqual(report["message"], "同步路径 {}".format(count))
            self.assertEqual(report["model_ids"], RISK_IDS)
            self.assertEqual(report["result"], context["result"])
            self.assertTrue(report["created_at"])
            self.assertTrue(report["prompt_version"])
            self.assertIn("/history/{}".format(report["id"]), response.get_data(as_text=True))
        self.assertEqual(self.database_snapshot(), self.initial_database)

    def test_reports_remain_readable_after_app_recreation(self):
        completed = self.complete_stream(message="跨应用实例保留的中文记录")[-1]
        expected = self.json_report(completed["record_id"])
        self.assertTrue((self.database_dir / "risk_reports.sqlite3").is_file())
        rebuilt_app = create_app()
        rebuilt_app.config.update(TESTING=True)
        response = rebuilt_app.test_client().get("/history/{}/export?format=json".format(completed["record_id"]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), expected)

    def test_history_paginates_and_searches_saved_messages(self):
        _, context = self.submit(message="分页公共样本")
        result = context["result"]
        for index in range(22):
            message = "专有关键词 {}".format(index) if index in (3, 17) else "普通记录 {}".format(index)
            reports.save_report(message, RISK_IDS, result)
        response = self.client.get("/history")
        self.assertEqual(response.status_code, 200)
        first = self.contexts[-1][1]
        self.assertEqual(first["total"], 23)
        self.assertEqual(first["total_pages"], 2)
        self.assertEqual(len(first["records"]), 20)
        self.client.get("/history?page=2")
        second = self.contexts[-1][1]
        self.assertEqual(len(second["records"]), 3)
        first_ids = {record["id"] for record in first["records"]}
        second_ids = {record["id"] for record in second["records"]}
        self.assertFalse(first_ids & second_ids)
        response = self.client.get("/history", query_string={"q": "专有关键词"})
        self.assertEqual(response.status_code, 200)
        matching = self.contexts[-1][1]
        self.assertEqual(matching["total"], 2)
        self.assertTrue(all("专有关键词" in record["message"] for record in matching["records"]))
        self.assertEqual(self.database_snapshot(), self.initial_database)

    def test_missing_report_and_unknown_export_format_are_not_success_pages(self):
        missing_id = "00000000-0000-0000-0000-000000000000"
        self.assertEqual(self.client.get("/history/" + missing_id).status_code, 404)
        for extension in ("json", "csv", "txt"):
            response = self.client.get("/history/{}/export?format={}".format(missing_id, extension))
            self.assertEqual(response.status_code, 404)
        record_id = self.complete_stream()[-1]["record_id"]
        response = self.client.get("/history/{}/export?format=exe".format(record_id))
        self.assertIn(response.status_code, (400, 404))

    def test_exports_preserve_unicode_and_protect_csv_formula_cells(self):
        message = '=公告,今夜停水\n请核实来源'
        reason = '=SUM(1,2)\n中文风险理由'
        for model, score in zip(self.model_instances.values(), (10, 50, 90)):
            model.check.return_value = (score, reason)
        record_id = self.complete_stream(message=message)[-1]["record_id"]
        record = self.json_report(record_id)
        self.assertEqual(record["message"], message)
        self.assertEqual(record["result"]["members"][0]["reason"], reason)
        responses = {}
        for extension in ("json", "csv", "txt"):
            response = self.client.get("/history/{}/export?format={}".format(record_id, extension))
            self.assertEqual(response.status_code, 200)
            self.assertNotEqual(response.mimetype, "text/html")
            self.assertIn("attachment", response.headers.get("Content-Disposition", ""))
            self.assertIn(record_id, response.headers["Content-Disposition"])
            self.assertIn("." + extension, response.headers["Content-Disposition"])
            self.assertNotIn(b"<!DOCTYPE", response.data)
            responses[extension] = response
        self.assertTrue(responses["csv"].data.startswith(b"\xef\xbb\xbf"))
        cells = [cell for row in csv.reader(io.StringIO(responses["csv"].data.decode("utf-8-sig"))) for cell in row]
        self.assertIn("'" + message, cells)
        self.assertIn("'" + reason, cells)
        for cell in cells:
            self.assertFalse(cell.startswith(("=", "+", "-", "@", "\t", "\r")), repr(cell))
        rows = list(csv.DictReader(io.StringIO(responses["csv"].data.decode("utf-8-sig"))))
        self.assertEqual([row["model_id"] for row in rows], RISK_IDS)
        for row in rows:
            self.assertEqual(row["decision_method"], "mean_fallback")
            self.assertEqual(float(row["mean_score"]), record["result"]["mean_score"])
            self.assertEqual(row["warning"], record["result"]["warning"])
            self.assertEqual(row["fallback_reason"], record["result"]["fallback_reason"])
            self.assertEqual(row["has_failures"], str(record["result"]["has_failures"]))
            for level in ("low", "medium", "high"):
                self.assertEqual(int(row["votes_" + level]), record["result"]["votes"][level])
        text_export = responses["txt"].data.decode("utf-8-sig")
        self.assertIn(message, text_export)
        self.assertIn(reason, text_export)
        self.assertIn(record["result"]["fallback_reason"], text_export)

    def test_history_list_and_detail_escape_original_message_and_model_reason(self):
        message = '<script>alert("stored-message")</script>'
        reason = '<img src=x onerror=alert("stored-reason")>'
        for model in self.model_instances.values():
            model.check.return_value = (80, reason)
        record_id = self.complete_stream(message=message)[-1]["record_id"]
        response = self.client.get("/history")
        listing = response.get_data(as_text=True)
        self.assertNotIn(message, listing)
        self.assertIn(str(escape(message)), listing)
        response = self.client.get("/history/" + record_id)
        self.assertEqual(response.status_code, 200)
        detail = response.get_data(as_text=True)
        for value in (message, reason):
            self.assertNotIn(value, detail)
            self.assertIn(str(escape(value)), detail)
        self.assertEqual(self.json_report(record_id)["message"], message)

    def test_storage_failure_still_shows_stream_result_without_fake_saved_id(self):
        with patch.object(reports, "save_report", side_effect=OSError("PRIVATE_DATABASE_PATH")):
            complete = self.complete_stream()[-1]
        self.assertIsNone(complete["record_id"])
        self.assertIsNone(complete["history_url"])
        self.assertTrue(complete["history_error"])
        self.assertIn("高风险", complete["html"])
        self.assertIn(complete["history_error"], complete["html"])
        self.assertNotIn("PRIVATE_DATABASE_PATH", json.dumps(complete, ensure_ascii=False))
        self.assertEqual(reports.list_reports()["total"], 0)

    def test_storage_failure_preserves_sync_result_and_safe_error(self):
        with patch.object(reports, "save_report", side_effect=OSError("PRIVATE_DATABASE_PATH")):
            response, context = self.submit("/detect")
        self.assertIsNone(context["error"])
        self.assertEqual(context["result"]["level"], "high")
        self.assertTrue(context["history_error"])
        self.assertIn(context["history_error"], response.get_data(as_text=True))
        self.assertNotIn("PRIVATE_DATABASE_PATH", response.get_data(as_text=True))
        self.assertEqual(reports.list_reports()["total"], 0)

    def test_history_read_failure_shows_safe_error_instead_of_success(self):
        with patch.object(reports, "list_reports", side_effect=OSError("PRIVATE_DATABASE_PATH")):
            response = self.client.get("/history")
        self.assertEqual(response.status_code, 503)
        error = self.contexts[-1][1]["error"]
        self.assertTrue(error)
        self.assertIn(error, response.get_data(as_text=True))
        self.assertNotIn("PRIVATE_DATABASE_PATH", response.get_data(as_text=True))
        with patch.object(reports, "get_report", side_effect=OSError("PRIVATE_DATABASE_PATH")):
            for url in ("/history/test-id", "/history/test-id/export?format=json"):
                with self.subTest(url=url):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 503)
                    self.assertIn("历史记录暂时无法读取", response.get_data(as_text=True))
                    self.assertNotIn("PRIVATE_DATABASE_PATH", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
