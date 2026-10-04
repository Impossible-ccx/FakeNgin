"""Available local-model choices and generic presentation in the web UI."""

from html.parser import HTMLParser
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from webapp import reports

import test_detect_routes as helpers


class VisibleText(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.hidden_depth = 0
        self.parts = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden_depth += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden_depth -= 1

    def handle_data(self, data):
        if not self.hidden_depth:
            self.parts.append(data)


class WebModelTests(unittest.TestCase):
    setUp = helpers.DetectRouteTests.setUp
    capture_template = helpers.DetectRouteTests.capture_template
    lookup_sources = helpers.DetectRouteTests.lookup_sources
    lookup_model = helpers.DetectRouteTests.lookup_model
    database_snapshot = helpers.DetectRouteTests.database_snapshot

    def test_only_available_models_are_listed_with_matching_default_mode_and_selection(self):
        for count in range(4):
            self.available_models.return_value = helpers.RISK_MODELS[:count]
            expected_ids = helpers.RISK_IDS[:count]
            for path in ("/detect", "/data"):
                with self.subTest(count=count, path=path):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 200)
                    context = self.contexts[-1][1]
                    self.assertEqual([model["id"] for model in context["models"]], expected_ids)
                    selection_key, mode_key = ("selected_ids", "mode") if path == "/detect" else ("default_selected_ids", "default_mode")
                    self.assertEqual(context[selection_key], expected_ids)
                    self.assertEqual(context[mode_key], "vote" if count > 1 else "single")
                    self.assertTrue(all(model["available"] for model in context["models"]))
            self.get_model.assert_not_called()

    def test_template_and_truth_classifier_never_appear_as_risk_choices(self):
        self.available_models.return_value = helpers.RISK_MODELS + [
            {"id": "template_model", "display_name": "Template", "description": "constant"},
            {"id": "roberta_rumor", "display_name": "RoBERTa", "description": "truth labels"},
        ]
        for path in ("/detect", "/data"):
            self.client.get(path)
            self.assertEqual([model["id"] for model in self.contexts[-1][1]["models"]], helpers.RISK_IDS)

    def test_pages_and_result_use_generic_model_labels_without_api_configuration_links(self):
        response = self.client.post("/detect", data={
            "message": "通用模型名称展示测试", "models": "deepseek_r1", "mode": "single",
        })
        self.assertEqual(response.status_code, 200)
        report_id = reports.list_reports()["records"][0]["id"]
        for path in ("/", "/detect", "/data", "/history", "/history/" + report_id):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                visible = " ".join(VisibleText(html).parts)
                for name in ("DeepSeek", "Qwen", "GLM", "RoBERTa"):
                    self.assertNotIn(name, visible)
                self.assertNotIn('href="/settings/api', html)
                if path in ("/detect", "/data", "/history/" + report_id):
                    self.assertRegex(visible, r"分析模型\s*[123]")

    def test_removed_api_settings_endpoints_return_404_without_inference(self):
        for method, path in (("get", "/settings/api"), ("post", "/settings/api"), ("post", "/settings/api/clear")):
            with self.subTest(method=method, path=path):
                response = getattr(self.client, method)(path)
                self.assertEqual(response.status_code, 404)
        self.get_model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
