"""风险投票规则测试；所有模型调用均替换为本地 mock。"""

from pathlib import Path
from copy import deepcopy
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from checkmodel.base import CheckError, RiskAbstention
from checkmodel import ensemble
from checkmodel.ensemble import MAX_MESSAGE_LENGTH, get_risk_models, run_risk_check


MODEL_IDS = ["qwen2.5_7b", "deepseek_r1", "glm4_9b"]


class RiskVotingTests(unittest.TestCase):
    def run_check(self, outputs, model_ids=None, mode="vote"):
        selected = MODEL_IDS if model_ids is None else model_ids

        def get_model(model_id):
            output = outputs[model_id]
            if isinstance(output, KeyError):
                raise output
            model = Mock(score_kind="risk")
            if isinstance(output, Exception):
                model.check.side_effect = output
            else:
                model.check.return_value = output
            return model

        with patch("checkmodel.ensemble.checkmodel.get_model", side_effect=get_model):
            return run_risk_check("待检测消息", selected, mode=mode)

    def scores(self, *scores):
        return {model_id: (score, "说明") for model_id, score in zip(MODEL_IDS, scores)}

    def test_majority_votes_on_levels_without_averaging(self):
        result = self.run_check(self.scores(99, 70, 0))
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["votes"], {"low": 1, "medium": 0, "high": 2})
        self.assertEqual(result["agreement_count"], 2)
        self.assertEqual(result["majority_required"], 2)
        self.assertEqual(result["success_count"], 3)
        self.assertFalse(result["has_failures"])
        self.assertEqual(result["decision_method"], "majority")
        self.assertIsNone(result["mean_score"])
        self.assertEqual(result["fallback_reason"], "")
        self.assertNotIn("score", result)
        self.assertNotIn("probability", result)

    def test_three_way_split_uses_arithmetic_mean(self):
        result = self.run_check(self.scores(10, 50, 90))
        self.assertEqual(result["level"], "medium")
        self.assertEqual(result["agreement_count"], 1)
        self.assertEqual(result["decision_method"], "mean_fallback")
        self.assertEqual(result["mean_score"], 50)
        self.assertIn("票数分歧", result["fallback_reason"])
        self.assertIn("算术平均", result["warning"])
        self.assertNotIn("无法判断", result["warning"])

    def test_all_errors_have_no_votes(self):
        result = self.run_check({model_id: CheckError("超时") for model_id in MODEL_IDS})
        self.assertEqual(result["level"], "uncertain")
        self.assertEqual(result["success_count"], 0)
        self.assertEqual(result["agreement_count"], 0)
        self.assertEqual(result["decision_method"], "unavailable")
        self.assertIsNone(result["mean_score"])
        self.assertTrue(result["has_failures"])
        self.assertTrue(all(member["score"] is None for member in result["members"]))

    def test_two_agree_with_error_preserves_selected_denominator(self):
        outputs = self.scores(80, 90, 10)
        outputs[MODEL_IDS[2]] = CheckError("模型超时")
        result = self.run_check(outputs)
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["selected_count"], 3)
        self.assertEqual(result["success_count"], 2)
        self.assertEqual(result["majority_required"], 2)
        self.assertEqual(result["decision_method"], "majority")
        self.assertIsNone(result["mean_score"])
        self.assertTrue(result["has_failures"])

    def test_only_one_success_uses_degraded_mean_not_majority(self):
        result = self.run_check({
            MODEL_IDS[0]: (90, "说明"),
            MODEL_IDS[1]: CheckError("超时"),
            MODEL_IDS[2]: KeyError("缺少模型"),
        })
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["selected_count"], 3)
        self.assertEqual(result["majority_required"], 2)
        self.assertEqual(result["members"][2]["status"], "unavailable")
        self.assertEqual(result["decision_method"], "mean_fallback")
        self.assertEqual(result["mean_score"], 90)
        self.assertIn("有效票不足", result["fallback_reason"])
        self.assertIn("1/3", result["fallback_reason"])
        self.assertIn("降级", result["warning"])

    def test_two_selected_models_disagree_and_use_mean(self):
        result = self.run_check(self.scores(90, 10), MODEL_IDS[:2])
        self.assertEqual(result["level"], "medium")
        self.assertEqual(result["majority_required"], 2)
        self.assertEqual(result["decision_method"], "mean_fallback")
        self.assertEqual(result["mean_score"], 50)

    def test_mean_denominator_excludes_errors_and_abstentions(self):
        for missing in [CheckError("超时"), KeyError("未安装"), RiskAbstention("信息不足")]:
            with self.subTest(missing=type(missing).__name__):
                outputs = self.scores(50, 90)
                outputs[MODEL_IDS[2]] = missing
                result = self.run_check(outputs)
                self.assertEqual(result["selected_count"], 3)
                self.assertEqual(result["success_count"], 2)
                self.assertEqual(result["mean_score"], 70)
                self.assertEqual(result["level"], "high")
                self.assertEqual(result["decision_method"], "mean_fallback")

    def test_mean_classifies_before_any_rounding(self):
        for left, right, expected_score, expected_level in [
            (10, 69.998, 39.999, "low"),
            (10, 70, 40, "medium"),
            (50, 89.998, 69.999, "medium"),
            (50, 90, 70, "high"),
        ]:
            with self.subTest(left=left, right=right):
                result = self.run_check(self.scores(left, right), MODEL_IDS[:2])
                self.assertEqual(result["decision_method"], "mean_fallback")
                self.assertAlmostEqual(result["mean_score"], expected_score)
                self.assertEqual(result["level"], expected_level)

    def test_invalid_score_is_excluded_from_fallback_mean(self):
        result = self.run_check(self.scores(50, 90, float("nan")))
        self.assertEqual(result["decision_method"], "mean_fallback")
        self.assertEqual(result["mean_score"], 70)
        self.assertEqual(result["success_count"], 2)
        self.assertEqual(result["members"][2]["status"], "error")

    def test_medium_majority(self):
        result = self.run_check(self.scores(45, 69, 80))
        self.assertEqual(result["level"], "medium")

    def test_original_float_controls_boundary(self):
        for score, expected in [(0, "low"), (39.999, "low"), (40, "medium"),
                                (69.999, "medium"), (70, "high"), (100, "high")]:
            with self.subTest(score=score):
                result = self.run_check(self.scores(score), MODEL_IDS[:1], mode="single")
                self.assertEqual(result["level"], expected)
                self.assertEqual(result["members"][0]["score"], score)

    def test_invalid_scores_do_not_become_votes(self):
        for score in [True, False, None, "not a score", float("nan"),
                      float("inf"), -float("inf"), -0.001, 100.001]:
            with self.subTest(score=score):
                result = self.run_check(self.scores(score), MODEL_IDS[:1], mode="single")
                self.assertEqual(result["members"][0]["status"], "error")
                self.assertEqual(result["success_count"], 0)
                self.assertEqual(result["level"], "uncertain")

    def test_reason_must_be_nonempty_string(self):
        for reason in ["", "  ", None, 1, ["说明"]]:
            with self.subTest(reason=reason):
                result = self.run_check({MODEL_IDS[0]: (50, reason)}, MODEL_IDS[:1], "single")
                self.assertEqual(result["members"][0]["status"], "error")

    def test_template_roberta_and_ensemble_are_rejected(self):
        for model_id in ["template_model", "roberta_rumor", "ensemble", "unknown"]:
            with self.subTest(model_id=model_id), self.assertRaises(ValueError):
                run_risk_check("消息", [MODEL_IDS[0], model_id])

    def test_duplicate_selections_cannot_create_votes(self):
        for selected in [[MODEL_IDS[0], MODEL_IDS[0]],
                         [MODEL_IDS[0], MODEL_IDS[0], MODEL_IDS[1]],
                         MODEL_IDS + [MODEL_IDS[0]]]:
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                run_risk_check("消息", selected)
        with self.assertRaises(ValueError):
            run_risk_check("消息", [MODEL_IDS[0], MODEL_IDS[0]], mode="single")

    def test_abstention_is_neither_vote_nor_runtime_failure(self):
        outputs = self.scores(80, 90)
        outputs[MODEL_IDS[2]] = RiskAbstention("消息信息不足")
        result = self.run_check(outputs)
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["success_count"], 2)
        self.assertEqual(result["selected_count"], 3)
        self.assertFalse(result["has_failures"])
        member = result["members"][2]
        self.assertEqual(member["status"], "abstained")
        self.assertEqual(member["reason"], "消息信息不足")
        self.assertIsNone(member["score"])
        self.assertIsNone(member["error"])

    def test_all_abstentions_are_uncertain(self):
        result = self.run_check({model_id: RiskAbstention("无法判断") for model_id in MODEL_IDS})
        self.assertEqual(result["level"], "uncertain")
        self.assertEqual(result["success_count"], 0)
        self.assertEqual(result["decision_method"], "unavailable")
        self.assertIsNone(result["mean_score"])
        self.assertFalse(result["has_failures"])

    def test_single_model_is_explicitly_marked(self):
        result = self.run_check(self.scores(90), MODEL_IDS[:1], mode="single")
        self.assertEqual(result["mode"], "single")
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["selected_count"], 1)
        self.assertEqual(result["majority_required"], 1)
        self.assertEqual(result["decision_method"], "single")
        self.assertIsNone(result["mean_score"])
        self.assertEqual(result["fallback_reason"], "")
        self.assertIn("单模型", result["warning"])

    def test_single_failure_has_unavailable_decision(self):
        result = self.run_check({MODEL_IDS[0]: CheckError("超时")}, MODEL_IDS[:1], "single")
        self.assertEqual(result["decision_method"], "unavailable")
        self.assertEqual(result["level"], "uncertain")
        self.assertIsNone(result["mean_score"])

    def test_unexpected_errors_do_not_expose_details(self):
        outputs = {MODEL_IDS[0]: RuntimeError("SECRET_API_TOKEN=do-not-show")}
        result = self.run_check(outputs, MODEL_IDS[:1], mode="single")
        self.assertEqual(result["members"][0]["status"], "error")
        self.assertNotIn("SECRET", str(result))

    def test_keyerror_during_inference_is_error_not_unavailable(self):
        model = Mock(score_kind="risk")
        model.check.side_effect = KeyError("sensitive response")
        with patch("checkmodel.ensemble.checkmodel.get_model", return_value=model):
            result = run_risk_check("消息", MODEL_IDS[:1], mode="single")
        self.assertEqual(result["members"][0]["status"], "error")
        self.assertNotIn("sensitive", str(result))
        model.check.assert_called_once_with("消息")

    def test_message_and_mode_validation_precedes_model_calls(self):
        invalid_requests = [
            ("", MODEL_IDS, "vote"), ("  ", MODEL_IDS, "vote"),
            (None, MODEL_IDS, "vote"), ("字" * (MAX_MESSAGE_LENGTH + 1), MODEL_IDS, "vote"),
            ("消息", MODEL_IDS, "unknown"), ("消息", [], "vote"),
            ("消息", MODEL_IDS[:1], "vote"), ("消息", MODEL_IDS, "single"),
            ("消息", MODEL_IDS[0], "single"), ("消息", [None], "single"),
        ]
        with patch("checkmodel.ensemble.checkmodel.get_model") as get_model:
            for message, selected, mode in invalid_requests:
                with self.subTest(message=str(message)[:20], selected=selected, mode=mode):
                    with self.assertRaises(ValueError):
                        run_risk_check(message, selected, mode)
            get_model.assert_not_called()

    def test_models_execute_in_selection_order_with_trimmed_message(self):
        calls = []

        def get_model(model_id):
            model = Mock(score_kind="risk")
            model.check.side_effect = lambda message: (calls.append((model_id, message)) or (20, "说明"))
            return model

        with patch("checkmodel.ensemble.checkmodel.get_model", side_effect=get_model):
            result = run_risk_check("  消息  ", list(reversed(MODEL_IDS)))
        self.assertEqual(calls, [(model_id, "消息") for model_id in reversed(MODEL_IDS)])
        self.assertGreaterEqual(result["elapsed_seconds"], 0)
        self.assertTrue(all(member["elapsed_seconds"] >= 0 for member in result["members"]))

    def test_model_listing_only_includes_available_risk_models(self):
        available = [{"id": MODEL_IDS[0]}, {"id": "template_model"}, {"id": "roberta_rumor"}]
        with patch("checkmodel.ensemble.checkmodel.get_models", return_value=available):
            models = get_risk_models()
        self.assertEqual([model["id"] for model in models], MODEL_IDS[:1])
        self.assertEqual([model["available"] for model in models], [True])
        self.assertTrue(all(model["display_name"] and model["description"] for model in models))

    def test_listing_does_not_mutate_factory_metadata_or_the_risk_registry(self):
        original_roster = deepcopy(ensemble.RISK_MODELS)
        original_lookup = deepcopy(ensemble._MODEL_BY_ID)
        factory_models = [{"id": MODEL_IDS[0]}, {"id": "roberta_rumor"}]
        original_factory_models = deepcopy(factory_models)
        with patch("checkmodel.ensemble.checkmodel.get_models", return_value=factory_models):
            models = get_risk_models()
        models[0]["display_name"] = "changed UI label"
        self.assertEqual(factory_models, original_factory_models)
        self.assertEqual(ensemble.RISK_MODELS, original_roster)
        self.assertEqual(ensemble._MODEL_BY_ID, original_lookup)
        with patch("checkmodel.ensemble.checkmodel.get_models", return_value=[]):
            self.assertEqual(get_risk_models(), [])
        result = self.run_check(self.scores(80, 90, 10))
        self.assertEqual(result["selected_count"], 3)
        self.assertEqual(result["level"], "high")

    def test_non_string_model_ids_are_rejected_before_any_model_lookup(self):
        for model_id in (None, True, 3, [], {}, [MODEL_IDS[0]]):
            with self.subTest(model_id=model_id):
                with patch("checkmodel.ensemble.checkmodel.get_model") as get_model:
                    with self.assertRaises(ValueError):
                        run_risk_check("消息", [model_id], mode="single")
                    get_model.assert_not_called()

    def test_probability_adapter_cannot_vote_even_under_a_known_risk_model_id(self):
        adapter = Mock(score_kind="probability", check=Mock(return_value=(99, "真假概率")))
        with patch("checkmodel.ensemble.checkmodel.get_model", return_value=adapter):
            result = run_risk_check("消息", MODEL_IDS[:1], mode="single")
        self.assertEqual(result["success_count"], 0)
        self.assertEqual(result["level"], "uncertain")
        self.assertEqual(result["members"][0]["status"], "error")
        self.assertIn("不提供风险评分", result["members"][0]["error"])
        adapter.check.assert_not_called()

    def test_listing_excludes_explicitly_non_risk_or_unavailable_adapters(self):
        available = [
            {"id": MODEL_IDS[0], "score_kind": "probability"},
            {"id": MODEL_IDS[1], "available": False},
            {"id": MODEL_IDS[2], "score_kind": "risk"},
        ]
        with patch("checkmodel.ensemble.checkmodel.get_models", return_value=available):
            self.assertEqual([model["id"] for model in get_risk_models()], MODEL_IDS[2:])


if __name__ == "__main__":
    unittest.main()
