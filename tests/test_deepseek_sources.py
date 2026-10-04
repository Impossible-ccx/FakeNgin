"""Web detection uses only local sources; CLI cloud support is tested separately."""

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
        # A configured owner model must never be selected by a web request.
        self.deepseek_sources[0]["available"] = True
        self.owner_cloud = Mock(check=Mock(return_value=(21, "站点私有模型")), source="cloud")
        self.deepseek_instances["cloud"] = self.owner_cloud
        self.local_model = self.deepseek_instances["local"]
        self.local_model.check.return_value = (84, "本地模型风险说明")

    def test_no_js_post_uses_local_and_preserves_source_in_report(self):
        _, context = self.submit("/detect", mode="single", models=["deepseek_r1"], deepseek_source="local")
        self.assertIsNone(context["error"])
        member = context["result"]["members"][0]
        self.assertEqual(member["source"], "local")
        self.assertEqual(member["score"], 84)
        self.get_model.assert_called_once_with("deepseek_r1", source="local")
        self.local_model.check.assert_called_once()
        self.owner_cloud.check.assert_not_called()
        stored = reports.list_reports()["records"][0]
        self.assertEqual(stored["result"]["members"][0], member)

    def test_omitted_source_cannot_select_configured_owner_cloud(self):
        _, context = self.submit("/detect/check", mode="single", models=["deepseek_r1"])
        self.assertEqual(context["result"]["members"][0]["source"], "local")
        self.assertEqual(context["result"]["members"][0]["score"], 84)
        self.owner_cloud.check.assert_not_called()
        self.get_model.assert_called_once_with("deepseek_r1", source="local")

    def test_cloud_or_invalid_sources_are_rejected_before_any_inference(self):
        for endpoint in ("/detect", "/detect/check", "/detect/stream"):
            for source in ("cloud", "both", "cloud,local", ""):
                with self.subTest(endpoint=endpoint, source=source):
                    response = self.client.post(endpoint, data=self.form(deepseek_source=source))
                    self.assertEqual(response.status_code, 400)
                    self.get_model.assert_not_called()
                    for model in (*self.model_instances.values(), self.owner_cloud):
                        model.check.assert_not_called()
                    self.assertEqual(reports.list_reports()["total"], 0)

    def test_unavailable_local_is_recorded_without_using_owner_cloud(self):
        self.deepseek_sources[1]["available"] = False
        _, context = self.submit("/detect/check", mode="single", models=["deepseek_r1"])
        member = context["result"]["members"][0]
        self.assertEqual(member["source"], "local")
        self.assertEqual(member["status"], "unavailable")
        self.assertIsNone(member["score"])
        self.owner_cloud.check.assert_not_called()
        self.local_model.check.assert_not_called()

    def test_stream_history_and_exports_keep_local_source_and_original_vote_count(self):
        events = self.complete_stream()
        starts = [event["member"] for event in events if event["type"] == "member_start"]
        completed = [event["member"] for event in events if event["type"] == "member_complete"]
        self.assertEqual([member["id"] for member in starts], route_helpers.RISK_IDS)
        self.assertEqual(len(completed), 3)
        self.assertTrue(all(member["source"] == "local" for member in completed))
        record_id = events[-1]["record_id"]
        record = self.json_report(record_id)
        self.assertEqual(record["result"]["selected_count"], 3)
        self.assertEqual(record["result"]["members"], [
            {key: value for key, value in member.items() if key != "ui_display_name"}
            for member in completed
        ])
        response = self.client.get("/history/{}/export?format=csv".format(record_id))
        rows = list(csv.DictReader(io.StringIO(response.data.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["source"] == "local" for row in rows))
        response = self.client.get("/history/{}/export?format=txt".format(record_id))
        self.assertIn("调用来源：本地 Ollama", response.get_data(as_text=True))
        self.owner_cloud.check.assert_not_called()
        self.assertEqual(self.database_snapshot(), self.initial_database)


if __name__ == "__main__":
    unittest.main()
