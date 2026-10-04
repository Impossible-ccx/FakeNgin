"""Complete report storage and exports using isolated storage and fake models."""

import csv
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from webapp import create_app, reports
import test_detect_routes as helpers


class HistoryRouteTests(unittest.TestCase):
    setUp = helpers.DetectRouteTests.setUp
    capture_template = helpers.DetectRouteTests.capture_template
    lookup_model = helpers.DetectRouteTests.lookup_model
    database_snapshot = helpers.DetectRouteTests.database_snapshot
    submit = helpers.DetectRouteTests.submit

    def test_sync_paths_save_separate_complete_reports_without_changing_dataset(self):
        before = self.database_snapshot()
        for endpoint in ("/detect", "/detect/check"):
            response, context = self.submit(endpoint, message="历史消息原文")
            self.assertIn("record", context)
            record = context["record"]
            self.assertEqual(record["message"], "历史消息原文")
            self.assertEqual(record["result"], context["result"])
            self.assertIn("/history/" + record["id"], response.get_data(as_text=True))
        self.assertEqual(reports.list_reports()["total"], 2)
        self.assertEqual(self.database_snapshot(), before)

    def test_invalid_input_is_not_saved_but_completed_failure_report_is(self):
        self.client.post("/detect", data={"message": "", "mode": "single", "models": helpers.RISK_IDS[0]})
        self.assertEqual(reports.list_reports()["total"], 0)
        self.model_instances.clear()
        _, context = self.submit()
        self.assertEqual(context["result"]["success_count"], 0)
        self.assertEqual(reports.list_reports()["total"], 1)

    def test_history_is_persistent_and_does_not_run_models_on_read(self):
        _, context = self.submit(message="保存过的消息")
        self.get_model.reset_mock()
        another = create_app().test_client()
        response = another.get("/history/" + context["record"]["id"])
        self.assertEqual(response.status_code, 200)
        self.assertIn("保存过的消息", response.get_data(as_text=True))
        self.get_model.assert_not_called()

    def test_history_pagination_and_literal_search(self):
        _, context = self.submit(message="100% _ 关键字")
        result = context["result"]
        for index in range(21):
            reports.save_report("其他消息 {}".format(index), helpers.RISK_IDS, result)
        self.assertEqual(reports.list_reports(page=1)["total_pages"], 2)
        self.assertEqual(len(reports.list_reports(page=999)["records"]), 2)
        for query in ("100%", "_"):
            self.assertEqual(reports.list_reports(query=query)["total"], 1)
        self.assertEqual(self.client.get("/history?q=关键字").status_code, 200)

    def test_exports_preserve_original_data_and_protect_csv_formula_cells(self):
        _, context = self.submit(message="=HYPERLINK(\"x\") 中文")
        record = context["record"]
        path = "/history/" + record["id"] + "/export"
        raw = self.client.get(path + "?format=json").get_json()
        self.assertEqual(raw["result"], record["result"])
        self.assertEqual(raw["message"], record["message"])
        text = self.client.get(path + "?format=txt").get_data(as_text=True)
        self.assertIn("中文", text)
        self.assertIn("分析模型 1", text)
        data = self.client.get(path + "?format=csv").data.decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(data)))
        self.assertEqual(len(rows), 4)
        self.assertTrue(any(cell.startswith("'=HYPERLINK") for cell in rows[1]))
        self.assertEqual(self.client.get(path + "?format=invalid").status_code, 400)
        self.assertEqual(self.client.get("/history/not-found").status_code, 404)

    def test_html_escapes_original_message_and_reason(self):
        attack = '<script>alert("HISTORY_XSS")</script>'
        for model in self.model_instances.values():
            model.check.return_value = (80, attack)
        _, context = self.submit(message=attack)
        for path in ("/history", "/history/" + context["record"]["id"]):
            html = self.client.get(path).get_data(as_text=True)
            self.assertNotIn(attack, html)
            self.assertIn("&lt;script&gt;", html)

    def test_storage_errors_preserve_current_result_and_show_safe_error(self):
        with patch.object(reports, "save_report", side_effect=RuntimeError("PRIVATE_PATH")):
            response, context = self.submit()
        self.assertEqual(context["result"]["level"], "high")
        self.assertIsNone(context["record"])
        self.assertIn("保存失败", response.get_data(as_text=True))
        self.assertNotIn("PRIVATE_PATH", response.get_data(as_text=True))
        with patch.object(reports, "list_reports", side_effect=RuntimeError("PRIVATE_PATH")):
            response = self.client.get("/history")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("PRIVATE_PATH", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
