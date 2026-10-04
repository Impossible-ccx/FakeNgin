"""Risk-detection HTTP integration tests; no Ollama or real CSV data required."""

from contextlib import ExitStack
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import checkmodel
from checkmodel.base import CheckError
from flask import template_rendered
from markupsafe import escape
from werkzeug.datastructures import MultiDict
from webapp import create_app, db, newsdata


RISK_MODELS = [
    {"id": "qwen2.5_7b", "display_name": "Qwen2.5-7B (Ollama)", "description": "Qwen risk model"},
    {"id": "deepseek_r1", "display_name": "DeepSeek-R1 (Ollama)", "description": "DeepSeek risk model"},
    {"id": "glm4_9b", "display_name": "GLM-4-9B (Ollama)", "description": "GLM risk model"},
]
RISK_IDS = [model["id"] for model in RISK_MODELS]


def risk_registry(available=(True, True, True), extra=()):
    """构造模型工厂 get_registered_models 的模拟返回值。"""
    entries = [
        {**model, "score_kind": "risk", "available": flag}
        for model, flag in zip(RISK_MODELS, available)
    ]
    return entries + list(extra)


def without_timings(value):
    """Compare full-page and fragment decisions without wall-clock variation."""
    if isinstance(value, dict):
        return {key: without_timings(item) for key, item in value.items() if key != "elapsed_seconds"}
    if isinstance(value, list):
        return [without_timings(item) for item in value]
    return value


class DetectRouteTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.database_dir = Path(self.stack.enter_context(TemporaryDirectory()))
        self.stack.enter_context(patch.multiple(
            db,
            DATABASE_DIR=self.database_dir,
            USERS_FILE=self.database_dir / "users.csv",
            SESSIONS_FILE=self.database_dir / "sessions.csv",
        ))
        self.stack.enter_context(patch.object(newsdata, "NEWSDATA_DIR", self.database_dir / "newsdata"))
        self.registered_models = self.stack.enter_context(patch.object(checkmodel, "get_registered_models", return_value=risk_registry()))
        self.model_instances = {
            model_id: Mock(check=Mock(return_value=(80, "需要核实具体消息来源。")))
            for model_id in RISK_IDS
        }
        self.get_model = self.stack.enter_context(patch.object(checkmodel, "get_model", side_effect=self.lookup_model))
        self.app = create_app()
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()
        self.contexts = []
        template_rendered.connect(self.capture_template, self.app, weak=False)
        self.addCleanup(template_rendered.disconnect, self.capture_template, self.app)
        self.initial_database = self.database_snapshot()

    def capture_template(self, sender, template, context, **extra):
        self.contexts.append((template.name, context))

    def lookup_model(self, model_id):
        return self.model_instances[model_id]

    def database_snapshot(self):
        return {str(path.relative_to(self.database_dir)): path.read_bytes() for path in self.database_dir.rglob("*.csv")}

    def configure_scores(self, scores):
        for model_id, score in zip(RISK_IDS, scores):
            self.model_instances[model_id].check.return_value = (score, "{} 的分析理由".format(model_id))

    def submit(self, endpoint="/detect/check", *, mode="vote", message="请评估这条消息的传播风险。", models=None):
        form = MultiDict([("mode", mode), ("message", message)])
        for model_id in RISK_IDS if models is None else models:
            form.add("models", model_id)
        response = self.client.post(endpoint, data=form)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.contexts)
        return response, self.contexts[-1][1]

    def test_get_with_no_models_keeps_all_choices_and_explains_unavailability(self):
        self.registered_models.return_value = risk_registry(available=(False, False, False))
        response = self.client.get("/detect")
        self.assertEqual(response.status_code, 200)
        context = self.contexts[-1][1]
        self.assertEqual(context["mode"], "vote")
        self.assertEqual(set(context["selected_ids"]), set(RISK_IDS))
        self.assertEqual(context["max_message_length"], 6000)
        self.assertEqual({model["id"] for model in context["models"]}, set(RISK_IDS))
        self.assertTrue(all(not model["available"] for model in context["models"]))
        html = response.get_data(as_text=True)
        for model in RISK_MODELS:
            self.assertIn(model["display_name"], html)
        self.assertRegex(html, "不可用|未就绪")
        self.assertIsNone(context["result"])
        self.get_model.assert_not_called()

    def test_risk_roster_excludes_template_and_truth_classifier(self):
        self.registered_models.return_value = risk_registry(extra=[
            {"id": "template_model", "display_name": "Template", "description": "constant output", "score_kind": "probability", "available": False},
            {"id": "roberta_rumor", "display_name": "RoBERTa", "description": "truth labels", "score_kind": "probability", "available": True},
        ])
        self.client.get("/detect")
        context = self.contexts[-1][1]
        self.assertEqual({model["id"] for model in context["models"]}, set(RISK_IDS))
        self.assertTrue(all(model["available"] for model in context["models"]))

    def test_vote_returns_majority_and_preserves_individual_reasons(self):
        self.configure_scores([85, 72, 28])
        response, context = self.submit()
        self.assertIsNone(context["error"])
        result = context["result"]
        self.assertEqual(result["mode"], "vote")
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["decision_method"], "majority")
        self.assertIsNone(result["mean_score"])
        self.assertEqual(result["votes"]["high"], 2)
        self.assertEqual(result["votes"]["low"], 1)
        self.assertEqual(result["majority_required"], 2)
        self.assertEqual(result["selected_count"], 3)
        self.assertEqual(result["success_count"], 3)
        self.assertEqual(result["agreement_count"], 2)
        self.assertFalse(result["has_failures"])
        self.assertEqual([member["score"] for member in result["members"]], [85, 72, 28])
        html = response.get_data(as_text=True)
        for model_id in RISK_IDS:
            self.model_instances[model_id].check.assert_called_once_with("请评估这条消息的传播风险。")
            self.assertIn("{} 的分析理由".format(model_id), html)
        self.assertEqual(self.database_snapshot(), self.initial_database)

    def test_single_mode_calls_only_selected_model_and_accepts_zero_score(self):
        self.model_instances[RISK_IDS[1]].check.return_value = (0, "暂未发现明显风险表达。")
        response, context = self.submit(mode="single", models=[RISK_IDS[1]])
        result = context["result"]
        self.assertIsNone(context["error"])
        self.assertEqual(result["mode"], "single")
        self.assertEqual(result["level"], "low")
        self.assertEqual(result["decision_method"], "single")
        self.assertIsNone(result["mean_score"])
        self.assertEqual(result["selected_count"], 1)
        self.assertEqual(result["members"][0]["score"], 0)
        self.get_model.assert_called_once_with(RISK_IDS[1])
        self.assertIn("暂未发现明显风险表达。", response.get_data(as_text=True))

    def test_all_three_levels_fall_back_to_mean_and_explain_disagreement(self):
        self.configure_scores([10, 50, 90])
        response, context = self.submit()
        result = context["result"]
        self.assertIsNone(context["error"])
        self.assertEqual(result["level"], "medium")
        self.assertEqual(result["decision_method"], "mean_fallback")
        self.assertEqual(result["mean_score"], 50)
        self.assertTrue(result["fallback_reason"])
        self.assertEqual(result["votes"], {"low": 1, "medium": 1, "high": 1})
        self.assertEqual(result["success_count"], 3)
        self.assertTrue(result["warning"])
        self.assertIn(result["warning"], response.get_data(as_text=True))
        self.assertIn(result["fallback_reason"], response.get_data(as_text=True))

    def test_one_runtime_failure_keeps_fixed_majority_and_visible_warning(self):
        self.model_instances[RISK_IDS[2]].check.side_effect = CheckError("测试模型调用超时")
        response, context = self.submit()
        result = context["result"]
        self.assertIsNone(context["error"])
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["majority_required"], 2)
        self.assertEqual(result["selected_count"], 3)
        self.assertEqual(result["success_count"], 2)
        self.assertTrue(result["has_failures"])
        self.assertEqual(result["votes"]["high"], 2)
        self.assertEqual(result["members"][2]["status"], "error")
        self.assertTrue(result["members"][2]["error"])
        html = response.get_data(as_text=True)
        self.assertIn(result["members"][2]["error"], html)
        self.assertIn(result["members"][2]["display_name"], html)

    def test_only_one_success_is_explicit_mean_fallback_with_original_count(self):
        for model_id in RISK_IDS[1:]:
            self.model_instances[model_id].check.side_effect = CheckError("测试模型暂不可用")
        response, context = self.submit()
        result = context["result"]
        self.assertEqual(result["mode"], "vote")
        self.assertEqual(result["selected_count"], 3)
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["majority_required"], 2)
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["decision_method"], "mean_fallback")
        self.assertEqual(result["mean_score"], 80)
        self.assertTrue(result["fallback_reason"])
        self.assertTrue(result["has_failures"])
        self.assertIn(result["fallback_reason"], response.get_data(as_text=True))

    def test_no_successful_scores_remains_unavailable(self):
        for model in self.model_instances.values():
            model.check.side_effect = CheckError("测试模型暂不可用")
        _, context = self.submit()
        result = context["result"]
        self.assertIsNone(context["error"])
        self.assertEqual(result["mode"], "vote")
        self.assertEqual(result["level"], "uncertain")
        self.assertEqual(result["decision_method"], "unavailable")
        self.assertIsNone(result["mean_score"])
        self.assertEqual(result["selected_count"], 3)
        self.assertEqual(result["success_count"], 0)
        self.assertTrue(result["has_failures"])

    def test_unavailable_selected_model_stays_in_denominator(self):
        self.registered_models.return_value = risk_registry(available=(True, True, False))
        del self.model_instances[RISK_IDS[2]]
        _, context = self.submit()
        result = context["result"]
        self.assertIsNone(context["error"])
        self.assertEqual(result["selected_count"], 3)
        self.assertEqual(result["success_count"], 2)
        self.assertEqual(result["majority_required"], 2)
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["members"][2]["status"], "unavailable")
        self.assertTrue(result["has_failures"])
        self.assertEqual([call.args[0] for call in self.get_model.call_args_list], RISK_IDS)

    def test_abstention_is_visible_and_does_not_count_as_medium_risk(self):
        from checkmodel.base import RiskAbstention

        self.model_instances[RISK_IDS[2]].check.side_effect = RiskAbstention("文本不足以评估传播风险")
        response, context = self.submit()
        result = context["result"]
        self.assertIsNone(context["error"])
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["votes"]["high"], 2)
        self.assertEqual(result["votes"]["medium"], 0)
        self.assertEqual(result["majority_required"], 2)
        self.assertEqual(result["members"][2]["status"], "abstained")
        self.assertIn("文本不足以评估传播风险", response.get_data(as_text=True))

    def test_two_selected_models_with_split_vote_use_arithmetic_mean(self):
        self.configure_scores([25, 80, 80])
        _, context = self.submit(models=RISK_IDS[:2])
        result = context["result"]
        self.assertIsNone(context["error"])
        self.assertEqual(result["selected_count"], 2)
        self.assertEqual(result["majority_required"], 2)
        self.assertEqual(result["level"], "medium")
        self.assertEqual(result["decision_method"], "mean_fallback")
        self.assertEqual(result["mean_score"], 52.5)
        self.model_instances[RISK_IDS[2]].check.assert_not_called()

    def test_invalid_requests_never_start_inference(self):
        invalid_requests = [
            {"message": ""},
            {"message": "   \n\t"},
            {"message": "风" * 6001},
            {"mode": "weighted"},
            {"models": []},
            {"models": [RISK_IDS[0]]},
            {"models": [RISK_IDS[0], "not-a-model"]},
            {"models": [RISK_IDS[0], RISK_IDS[0], RISK_IDS[1]]},
            {"models": [RISK_IDS[0], "template_model"]},
            {"models": [RISK_IDS[0], "roberta_rumor"]},
            {"mode": "single", "models": RISK_IDS[:2]},
        ]
        for endpoint in ("/detect", "/detect/check"):
            for arguments in invalid_requests:
                with self.subTest(endpoint=endpoint, arguments=arguments):
                    self.get_model.reset_mock()
                    response, context = self.submit(endpoint, **arguments)
                    self.assertTrue(context["error"])
                    self.assertIsNone(context["result"])
                    self.assertIn(str(escape(context["error"])), response.get_data(as_text=True))
                    self.get_model.assert_not_called()

    def test_message_at_length_limit_is_accepted_without_truncation(self):
        message = "风" * 6000
        _, context = self.submit(message=message)
        self.assertIsNone(context["error"])
        for model in self.model_instances.values():
            model.check.assert_called_once_with(message)

    def test_input_and_model_reason_are_html_escaped(self):
        message = "<script>alert('input')</script>"
        reason = "<img src=x onerror=alert('reason')>"
        for model in self.model_instances.values():
            model.check.return_value = (80, reason)
        response, context = self.submit("/detect", message=message)
        self.assertIsNone(context["error"])
        html = response.get_data(as_text=True)
        self.assertNotIn(message, html)
        self.assertIn(str(escape(message)), html)
        self.assertNotIn(reason, html)
        self.assertIn(str(escape(reason)), html)
        response, _ = self.submit(message=message)
        fragment = response.get_data(as_text=True)
        self.assertNotIn(reason, fragment)
        self.assertIn(str(escape(reason)), fragment)

    def test_full_page_and_async_fragment_show_same_decision_without_saving(self):
        self.configure_scores([15, 20, 80])
        with patch.object(newsdata, "append_message") as append_message:
            full_response, full_context = self.submit("/detect")
            fragment_response, fragment_context = self.submit("/detect/check")
        self.assertEqual(without_timings(full_context["result"]), without_timings(fragment_context["result"]))
        full_html = full_response.get_data(as_text=True)
        fragment_html = fragment_response.get_data(as_text=True)
        self.assertIn("<html", full_html.lower())
        self.assertNotIn("<html", fragment_html.lower())
        for html in (full_html, fragment_html):
            self.assertIn(full_context["result"]["label"], html)
            self.assertNotIn("/detect/save", html)
            self.assertNotIn("保存到数据集", html)
        # 整页的「分类器检测」模式选项按设计提及虚假概率（真假分类分数）；
        # 风险检测结果片段本身仍不得出现真假概率字样。
        self.assertNotIn("虚假概率", fragment_html)
        append_message.assert_not_called()
        self.assertEqual(self.database_snapshot(), self.initial_database)


if __name__ == "__main__":
    unittest.main()
