"""Explicit DeepSeek source selection across HTTP, streaming, storage and export."""

import csv
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from webapp import reports

import test_detect_routes as route_helpers
import test_history_stream as stream_helpers


class DeepSeekSourceRouteTests(unittest.TestCase):
    capture_template = route_helpers.DetectRouteTests.capture_template
    lookup_sources = route_helpers.DetectRouteTests.lookup_sources
    lookup_model = route_helpers.DetectRouteTests.lookup_model
    database_snapshot = route_helpers.DetectRouteTests.database_snapshot
    submit = route_helpers.DetectRouteTests.submit
    form = stream_helpers.HistoryStreamTests.form
    events = stream_helpers.HistoryStreamTests.events
    complete_stream = stream_helpers.HistoryStreamTests.complete_stream
    json_report = stream_helpers.HistoryStreamTests.json_report

    def setUp(self):
        route_helpers.DetectRouteTests.setUp(self)
        self.deepseek_sources[0]["available"] = True
        self.cloud_model = Mock(
            check=Mock(return_value=(21, "云端模型的风险说明")),
            display_name=self.deepseek_sources[0]["display_name"],
            source="cloud", source_label="云端 API",
        )
        self.deepseek_instances["cloud"] = self.cloud_model
        self.local_model = self.deepseek_instances["local"]
        self.local_model.display_name = self.deepseek_sources[1]["display_name"]
        self.local_model.check.return_value = (84, "本地模型的风险说明")

    def test_get_offers_both_sources_in_one_deepseek_slot(self):
        response = self.client.get("/detect")
        self.assertEqual(response.status_code, 200)
        context = self.contexts[-1][1]
        self.assertEqual(context["deepseek_source"], "cloud")
        matching = [model for model in context["models"] if model["id"] == "deepseek_r1"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["default_source"], "cloud")
        self.assertEqual({item["source"] for item in matching[0]["sources"]}, {"cloud", "local"})
        html = response.get_data(as_text=True)
        self.assertIn('name="deepseek_source"', html)
        self.assertIn("云端 API", html)
        self.assertIn("本地 Ollama", html)
        self.get_model.assert_not_called()

    def test_no_js_post_honors_each_source_and_preserves_it_in_report(self):
        for source, expected_score, selected, other in (
            ("cloud", 21, self.cloud_model, self.local_model),
            ("local", 84, self.local_model, self.cloud_model),
        ):
            with self.subTest(source=source):
                self.cloud_model.check.reset_mock()
                self.local_model.check.reset_mock()
                self.get_model.reset_mock()
                response, context = self.submit("/detect", mode="single", models=["deepseek_r1"], deepseek_source=source)
                self.assertIsNone(context["error"])
                self.assertEqual(context["deepseek_source"], source)
                member = context["result"]["members"][0]
                self.assertEqual(member["source"], source)
                self.assertEqual(member["source_label"], selected.source_label)
                self.assertEqual(member["score"], expected_score)
                self.assertEqual(context["result"]["selected_count"], 1)
                self.get_model.assert_called_once_with("deepseek_r1", source=source)
                selected.check.assert_called_once()
                other.check.assert_not_called()
                stored = reports.list_reports()["records"][0]
                self.assertEqual(stored["model_ids"], ["deepseek_r1"])
                self.assertEqual(stored["result"]["members"][0], member)
                self.assertIn(member["display_name"], response.get_data(as_text=True))

    def test_omitted_source_keeps_legacy_default_after_explicit_local_request(self):
        self.submit("/detect", mode="single", models=["deepseek_r1"], deepseek_source="local")
        self.cloud_model.check.reset_mock()
        self.local_model.check.reset_mock()
        _, context = self.submit("/detect/check", mode="single", models=["deepseek_r1"])
        self.assertEqual(context["result"]["members"][0]["source"], "cloud")
        self.assertEqual(context["result"]["members"][0]["score"], 21)
        self.cloud_model.check.assert_called_once()
        self.local_model.check.assert_not_called()

    def test_unavailable_selected_source_is_recorded_without_using_other_source(self):
        self.deepseek_sources[1]["available"] = False
        _, context = self.submit("/detect/check", deepseek_source="local")
        result = context["result"]
        member = next(member for member in result["members"] if member["id"] == "deepseek_r1")
        self.assertEqual(member["source"], "local")
        self.assertEqual(member["status"], "unavailable")
        self.assertIsNone(member["score"])
        self.assertEqual(result["selected_count"], 3)
        self.assertEqual(result["success_count"], 2)
        self.cloud_model.check.assert_not_called()
        self.local_model.check.assert_not_called()
        stored = reports.list_reports()["records"][0]
        self.assertEqual(stored["result"]["members"][1]["source"], "local")

    def test_invalid_source_is_rejected_by_all_endpoints_before_any_model_call(self):
        for endpoint in ("/detect", "/detect/check", "/detect/stream"):
            for invalid_source in ("both", "cloud,local", ""):
                with self.subTest(endpoint=endpoint, source=invalid_source):
                    response = self.client.post(endpoint, data=self.form(deepseek_source=invalid_source))
                    self.assertEqual(response.status_code, 400)
                    if endpoint == "/detect/stream":
                        self.assertEqual(response.get_json()["type"], "error")
                    else:
                        self.assertTrue(self.contexts[-1][1]["error"])
                    self.get_model.assert_not_called()
                    self.cloud_model.check.assert_not_called()
                    self.local_model.check.assert_not_called()
                    self.assertEqual(reports.list_reports()["total"], 0)

    def test_stream_history_and_exports_keep_source_without_extra_vote(self):
        for source in ("cloud", "local"):
            with self.subTest(source=source):
                events = self.complete_stream(deepseek_source=source)
                starts = [event["member"] for event in events if event["type"] == "member_start"]
                completed = [event["member"] for event in events if event["type"] == "member_complete"]
                self.assertEqual([member["id"] for member in starts], route_helpers.RISK_IDS)
                self.assertEqual(len(completed), 3)
                self.assertEqual(starts[1]["source"], source)
                self.assertEqual(completed[1]["source"], source)
                self.assertEqual(starts[1]["source_label"], completed[1]["source_label"])
                record_id = events[-1]["record_id"]
                record = self.json_report(record_id)
                self.assertEqual(record["model_ids"], route_helpers.RISK_IDS)
                self.assertEqual(record["result"]["selected_count"], 3)
                self.assertEqual(record["result"]["members"], completed)
                csv_response = self.client.get("/history/{}/export?format=csv".format(record_id))
                rows = list(csv.DictReader(io.StringIO(csv_response.data.decode("utf-8-sig"))))
                self.assertEqual(len(rows), 3)
                deepseek_row = next(row for row in rows if row["model_id"] == "deepseek_r1")
                self.assertEqual(deepseek_row["source"], source)
                self.assertEqual(deepseek_row["source_label"], completed[1]["source_label"])
                text_response = self.client.get("/history/{}/export?format=txt".format(record_id))
                self.assertIn("调用来源：" + completed[1]["source_label"], text_response.get_data(as_text=True))
                detail_response = self.client.get("/history/" + record_id)
                self.assertEqual(detail_response.status_code, 200)
                self.assertIn(completed[1]["source_label"], detail_response.get_data(as_text=True))
        self.assertEqual(self.database_snapshot(), self.initial_database)


if __name__ == "__main__":
    unittest.main()
