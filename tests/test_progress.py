"""Real event ordering and complete-report persistence with local mock models."""

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from checkmodel.base import CheckError, RiskAbstention
from checkmodel.ensemble import iter_risk_check, run_risk_check, validate_risk_request
from werkzeug.datastructures import MultiDict
from webapp import reports
from webapp.routes import detect as detect_routes

import test_detect_routes as helpers

RISK_IDS = helpers.RISK_IDS


class ProgressTests(unittest.TestCase):
    setUp = helpers.DetectRouteTests.setUp
    capture_template = helpers.DetectRouteTests.capture_template
    lookup_model = helpers.DetectRouteTests.lookup_model
    database_snapshot = helpers.DetectRouteTests.database_snapshot
    configure_scores = helpers.DetectRouteTests.configure_scores

    def form(self, message="待分析的消息", model_ids=None, mode="vote"):
        form = MultiDict([("message", message), ("mode", mode)])
        for model_id in RISK_IDS if model_ids is None else model_ids:
            form.add("models", model_id)
        return form

    def stream(self, **kwargs):
        response = self.client.post("/detect/stream", data=self.form(**kwargs), buffered=False)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/x-ndjson")
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(response.headers["X-Accel-Buffering"], "no")
        return response

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

    def complete(self, **kwargs):
        response = self.stream(**kwargs)
        try:
            events = list(self.events(response))
        finally:
            response.close()
        self.assertEqual(events[-1]["type"], "complete")
        return events

    def test_iterator_announces_start_before_model_lookup_and_before_inference(self):
        events = iter_risk_check("  原始消息  ", list(reversed(RISK_IDS)))
        completed = []
        for index, model_id in enumerate(reversed(RISK_IDS)):
            started = next(events)
            self.assertEqual(started["type"], "member_start")
            self.assertEqual(started["member"]["id"], model_id)
            self.assertEqual(started["member"]["status"], "running")
            self.assertEqual(self.get_model.call_count, index)
            self.model_instances[model_id].check.assert_not_called()
            returned = next(events)
            self.assertEqual(returned["type"], "member_complete")
            self.model_instances[model_id].check.assert_called_once_with("原始消息")
            completed.append(returned["member"])
        final = next(events)
        self.assertEqual(final["type"], "complete")
        self.assertEqual(final["result"]["members"], completed)
        with self.assertRaises(StopIteration):
            next(events)

    def test_http_stream_delivers_live_events_and_saves_one_raw_complete_report(self):
        self.configure_scores([10, 50, 90])
        response = self.stream()
        received = self.events(response)
        completed = []
        with patch.object(reports, "save_report", wraps=reports.save_report) as save:
            try:
                for index, model_id in enumerate(RISK_IDS):
                    started = next(received)
                    self.assertEqual(started["type"], "member_start")
                    self.assertEqual(started["member"]["ui_display_name"], "分析模型 {}".format(index + 1))
                    self.assertEqual(self.get_model.call_count, index)
                    self.model_instances[model_id].check.assert_not_called()
                    returned = next(received)
                    self.assertEqual(returned["type"], "member_complete")
                    self.assertEqual(returned["member"]["id"], model_id)
                    completed.append(returned["member"])
                    save.assert_not_called()
                final = next(received)
                self.assertEqual(final["type"], "complete")
                self.assertIn("data-risk-result", final["html"])
                self.assertTrue(final["record_id"])
                self.assertEqual(final["history_url"], "/history/" + final["record_id"])
                self.assertIsNone(final["history_error"])
                save.assert_called_once()
                with self.assertRaises(StopIteration):
                    next(received)
            finally:
                response.close()
        stored = reports.get_report(final["record_id"])
        self.assertEqual(stored["result"]["decision_method"], "mean_fallback")
        self.assertEqual(stored["result"]["mean_score"], 50)
        self.assertEqual(stored["result"]["members"], [
            {key: value for key, value in member.items() if key != "ui_display_name"}
            for member in completed
        ])
        self.assertEqual(reports.list_reports()["total"], 1)
        self.assertEqual(self.database_snapshot(), self.initial_database)

    def test_disconnecting_before_the_first_model_stops_without_inference_or_saving(self):
        response = self.stream()
        self.get_model.assert_not_called()
        response.close()
        self.get_model.assert_not_called()
        self.assertEqual(reports.list_reports()["total"], 0)

    def test_disconnecting_between_members_does_not_run_remaining_models_or_save_partial_results(self):
        response = self.stream()
        received = self.events(response)
        self.assertEqual(next(received)["type"], "member_start")
        self.assertEqual(next(received)["type"], "member_complete")
        response.close()
        self.model_instances[RISK_IDS[0]].check.assert_called_once()
        for model_id in RISK_IDS[1:]:
            self.model_instances[model_id].check.assert_not_called()
        self.assertEqual(self.get_model.call_count, 1)
        self.assertEqual(reports.list_reports()["total"], 0)

    def test_disconnecting_after_all_member_events_does_not_save_without_completion(self):
        response = self.stream()
        received = self.events(response)
        for _ in RISK_IDS:
            self.assertEqual(next(received)["type"], "member_start")
            self.assertEqual(next(received)["type"], "member_complete")
        response.close()
        self.assertEqual(self.get_model.call_count, 3)
        self.assertEqual(reports.list_reports()["total"], 0)

    def test_invalid_requests_are_rejected_before_streaming_or_model_calls(self):
        invalid_forms = [
            self.form(message=""), self.form(message="字" * 6001),
            self.form(model_ids=[]), self.form(model_ids=[RISK_IDS[0], "unknown"]),
            self.form(model_ids=[RISK_IDS[0], "roberta_rumor"]),
            self.form(model_ids=[RISK_IDS[0], RISK_IDS[0]]),
            self.form(mode="weighted"), self.form(model_ids=[RISK_IDS[0]], mode="vote"),
        ]
        for form in invalid_forms:
            with self.subTest(form=form):
                response = self.client.post("/detect/stream", data=form)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.mimetype, "application/json")
                self.assertEqual(response.get_json()["type"], "error")
                self.get_model.assert_not_called()
        self.assertEqual(reports.list_reports()["total"], 0)

    def test_failures_and_abstentions_are_excluded_from_average_but_continue_stream(self):
        self.model_instances[RISK_IDS[0]].check.return_value = (90, "风险说明")
        self.model_instances[RISK_IDS[1]].check.side_effect = RuntimeError("PRIVATE_MODEL_RESPONSE")
        self.model_instances[RISK_IDS[2]].check.side_effect = RiskAbstention("上下文不足")
        events = self.complete()
        returned = [event["member"] for event in events if event["type"] == "member_complete"]
        self.assertEqual([member["status"] for member in returned], ["ok", "error", "abstained"])
        self.assertNotIn("PRIVATE_MODEL_RESPONSE", json.dumps(events, ensure_ascii=False))
        result = reports.get_report(events[-1]["record_id"])["result"]
        self.assertEqual((result["selected_count"], result["success_count"], result["majority_required"]), (3, 1, 2))
        self.assertEqual((result["decision_method"], result["mean_score"], result["level"]), ("mean_fallback", 90, "high"))

    def test_all_failed_members_form_a_complete_saved_report(self):
        for model in self.model_instances.values():
            model.check.side_effect = CheckError("模型暂不可用")
        events = self.complete()
        result = reports.get_report(events[-1]["record_id"])["result"]
        self.assertEqual(result["success_count"], 0)
        self.assertEqual(result["decision_method"], "unavailable")
        self.assertEqual(reports.list_reports()["total"], 1)

    def test_stream_and_sync_share_exact_decisions_except_elapsed_time(self):
        for scores in ([80, 90, 10], [10, 50, 90], [0, 39.999, 40], [40, 69.999, 70]):
            with self.subTest(scores=scores):
                self.configure_scores(scores)
                sync = run_risk_check("待分析的消息", RISK_IDS)
                events = self.complete()
                streamed = reports.get_report(events[-1]["record_id"])["result"]
                self.assertEqual(helpers.without_timings(sync), helpers.without_timings(streamed))

    def test_history_storage_failure_preserves_current_complete_result(self):
        with patch.object(reports, "save_report", side_effect=RuntimeError("PRIVATE_STORAGE_PATH")) as save:
            events = self.complete(model_ids=RISK_IDS[:1], mode="single")
        self.assertEqual(save.call_count, 1)
        final = events[-1]
        self.assertIsNone(final["record_id"])
        self.assertIsNone(final["history_url"])
        self.assertIn("保存失败", final["history_error"])
        self.assertIn("检测结果", final["html"])
        self.assertNotIn("PRIVATE_STORAGE_PATH", final["html"])

    def test_unexpected_iterator_error_is_sanitized_and_does_not_save_partial_report(self):
        def interrupted(*args):
            yield {"type": "member_start", "member": {"id": RISK_IDS[0], "status": "running"}}
            raise RuntimeError("PRIVATE_ITERATOR_ERROR")
        with patch.object(detect_routes, "iter_risk_check", side_effect=interrupted):
            response = self.stream()
            try:
                events = list(self.events(response))
            finally:
                response.close()
        self.assertEqual(events[-1]["type"], "error")
        self.assertNotIn("PRIVATE_ITERATOR_ERROR", str(events))
        self.assertEqual(reports.list_reports()["total"], 0)
        self.get_model.assert_not_called()

    def test_validation_helper_is_local_and_does_not_resolve_a_model(self):
        self.assertEqual(validate_risk_request("  消息  ", RISK_IDS[:1], "single"), ("消息", RISK_IDS[:1]))
        self.get_model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
