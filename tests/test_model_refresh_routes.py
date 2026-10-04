"""模型状态查看与用户主动连接检查；服务和模型均使用 mock。"""

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from webapp import batches, reports
import test_detect_routes as helpers


class ModelRefreshRouteTests(unittest.TestCase):
    setUp = helpers.DetectRouteTests.setUp
    capture_template = helpers.DetectRouteTests.capture_template
    lookup_model = helpers.DetectRouteTests.lookup_model
    database_snapshot = helpers.DetectRouteTests.database_snapshot

    def unknown_models(self):
        self.cached_models.side_effect = lambda model_ids=None: [
            {**model, "available": None, "score_kind": "risk"}
            for model in helpers.RISK_MODELS
            if model_ids is None or model["id"] in model_ids
        ]

    def test_data_get_renders_unknown_models_without_probe_or_initialization(self):
        self.unknown_models()
        response = self.client.get("/data")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("尚未检查", html)
        self.assertIn("检查模型连接", html)
        self.assertNotIn("重启网站", html)
        for model in helpers.RISK_MODELS:
            self.assertNotIn(model["display_name"], html)
        context = self.contexts[-1][1]
        self.assertEqual(context["default_mode"], "single")
        self.assertEqual(context["default_selected_ids"], [])
        self.assertTrue(all(model["needs_probe"] for model in context["models"]))
        self.available_models.assert_not_called()
        self.get_model.assert_not_called()

    def test_model_status_get_returns_snapshot_without_probe(self):
        self.unknown_models()
        response = self.client.get("/data/models")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        body = response.get_json()
        self.assertEqual(body["available_count"], 0)
        self.assertTrue(body["needs_probe"])
        self.assertEqual(len(body["models"]), 3)
        self.assertTrue(all(model["available"] is None for model in body["models"]))
        self.available_models.assert_not_called()
        self.get_model.assert_not_called()

    def test_explicit_refresh_returns_available_models_without_inference_or_records(self):
        self.available_models.return_value = [
            {**model, "available": True, "score_kind": "risk"}
            for model in helpers.RISK_MODELS[:2]
        ]
        with patch.object(reports, "save_report") as save, patch.object(batches, "create_batch") as create:
            response = self.client.post("/data/models/refresh", headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["available_count"], 2)
        self.assertFalse(body["needs_probe"])
        self.assertEqual([model["id"] for model in body["models"]], helpers.RISK_IDS[:2])
        self.assertTrue(all(model["display_name"].startswith("分析模型") for model in body["models"]))
        self.available_models.assert_called_once_with(model_ids=helpers.RISK_IDS, refresh=True)
        self.get_model.assert_not_called()
        save.assert_not_called()
        create.assert_not_called()
        self.assertEqual(self.database_snapshot(), self.initial_database)

    def test_empty_refresh_can_recover_after_service_starts_without_restart(self):
        self.unknown_models()
        self.available_models.return_value = []
        first = self.client.post("/data/models/refresh", headers={"Accept": "application/json"})
        self.assertEqual(first.get_json()["available_count"], 0)
        self.available_models.return_value = [{**helpers.RISK_MODELS[0], "available": True}]
        second = self.client.post("/data/models/refresh", headers={"Accept": "application/json"})
        self.assertEqual(second.get_json()["available_count"], 1)
        self.assertEqual(self.available_models.call_count, 2)
        self.get_model.assert_not_called()

    def test_native_refresh_form_keeps_page_and_redirects_without_inference(self):
        response = self.client.post("/data/models/refresh", data={"page": "2"})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "/data?page=2")
        self.available_models.assert_called_once_with(model_ids=helpers.RISK_IDS, refresh=True)
        self.get_model.assert_not_called()

    def test_snapshot_defaults_follow_confirmed_available_models(self):
        for size in (0, 1, 2, 3):
            with self.subTest(size=size):
                self.available_models.return_value = helpers.RISK_MODELS[:size]
                response = self.client.get("/data")
                self.assertEqual(response.status_code, 200)
                context = self.contexts[-1][1]
                self.assertEqual(context["default_selected_ids"], helpers.RISK_IDS[:size])
                self.assertEqual(context["default_mode"], "vote" if size > 1 else "single")
        self.available_models.assert_not_called()
        self.get_model.assert_not_called()

    def test_four_registered_candidates_are_refreshed_and_displayed_with_three_default_selections(self):
        fourth_id = helpers.register_fourth_adapter(self)
        response = self.client.get("/data")
        self.assertEqual(response.status_code, 200)
        context = self.contexts[-1][1]
        self.assertEqual(len(context["models"]), 4)
        self.assertEqual(context["default_selected_ids"], helpers.RISK_IDS)
        self.assertEqual(context["default_mode"], "vote")
        self.available_models.assert_not_called()
        refreshed = self.client.post("/data/models/refresh", headers={"Accept": "application/json"})
        self.assertEqual(refreshed.get_json()["available_count"], 4)
        self.assertEqual(refreshed.get_json()["models"][-1]["display_name"], "分析模型 4")
        self.available_models.assert_called_once_with(model_ids=helpers.RISK_IDS + [fourth_id], refresh=True)
        self.get_model.assert_not_called()

    def test_failed_refresh_has_safe_error_and_does_not_fall_back_to_probe_on_render(self):
        self.available_models.side_effect = RuntimeError("PRIVATE_HOST_ADDRESS")
        response = self.client.post("/data/models/refresh", headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("PRIVATE_HOST_ADDRESS", response.get_data(as_text=True))
        self.assertEqual(self.available_models.call_count, 1)
        self.get_model.assert_not_called()

    def test_status_endpoint_cannot_be_used_to_refresh_through_get(self):
        self.assertEqual(self.client.get("/data/models/refresh").status_code, 405)
        self.available_models.assert_not_called()
        self.get_model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
