"""DeepSeek API tests with fake credentials and mocked HTTP; never read .env."""

from contextlib import ExitStack, redirect_stderr
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import checkmodel
from checkmodel import deepseek_api
from checkmodel.base import CheckError, RiskAbstention
from checkmodel.ensemble import get_risk_models, run_risk_check
from checkmodel.ollama_deepseek import Ollama_DeepSeek


FAKE_KEY = "test-only-never-a-real-api-key"


class DeepSeekAPITests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {
            "DEEPSEEK_API_KEY": FAKE_KEY,
            "DEEPSEEK_MODEL": "deepseek-flash",
        }, clear=True))
        self.opener = MagicMock()
        self.build_opener = self.stack.enter_context(patch.object(deepseek_api, "build_opener", return_value=self.opener))

    def respond(self, content=None, payload=None):
        if content is None:
            content = json.dumps(payload or {"risk_score": 72, "reason": "缺少可追溯来源"}, ensure_ascii=False)
        body = json.dumps({"choices": [{"message": {"content": content}}]}, ensure_ascii=False).encode("utf-8")
        self.opener.open.return_value.__enter__.return_value.read.return_value = body

    def test_valid_request_preserves_unicode_score_and_uses_configured_model(self):
        self.respond(payload={"risk_score": 39.99, "reason": "来源描述较明确"})
        model = deepseek_api.DeepSeekAPI()
        self.assertTrue(model.detect())
        self.opener.open.assert_not_called()
        self.assertEqual(model.check("中文消息：今天 {天气} 晴朗。"), (39.99, "来源描述较明确"))
        self.opener.open.assert_called_once()
        request = self.opener.open.call_args.args[0]
        self.assertTrue(request.full_url.startswith("https://api.deepseek.com/"))
        self.assertTrue(request.full_url.endswith("/chat/completions"))
        self.assertEqual(request.get_header("Authorization"), "Bearer " + FAKE_KEY)
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertTrue(any("中文消息：今天 {天气} 晴朗。" in message["content"] for message in payload["messages"]))
        self.assertNotIn(FAKE_KEY, json.dumps(payload, ensure_ascii=False))
        self.assertGreater(self.opener.open.call_args.kwargs["timeout"], 0)

    def test_missing_key_is_unavailable_without_attempting_http(self):
        for key in ("", "   "):
            with self.subTest(key=key), patch.dict(os.environ, {"DEEPSEEK_API_KEY": key}):
                model = deepseek_api.DeepSeekAPI()
                self.assertFalse(model.detect())
                with self.assertRaises(CheckError):
                    model.check("测试")
        self.opener.open.assert_not_called()

    def test_risk_boundaries_remain_numeric_scores(self):
        for score in (0, 40, 69.99, 70, 100):
            with self.subTest(score=score):
                self.respond(payload={"risk_score": score, "reason": "风险说明"})
                self.assertEqual(deepseek_api.DeepSeekAPI().check("测试消息"), (score, "风险说明"))

    def test_null_score_abstains_without_retry_or_fabricated_score(self):
        self.respond(payload={"risk_score": None, "reason": " 上下文不足 "})
        with self.assertRaisesRegex(RiskAbstention, "上下文不足"):
            deepseek_api.DeepSeekAPI().check("这条消息是真的吗")
        self.opener.open.assert_called_once()

    def test_invalid_or_legacy_json_is_rejected_without_revealing_content(self):
        payloads = [
            {"probability": 99, "reason": "旧协议"},
            {"risk_score": -1, "reason": "越界"},
            {"risk_score": 101, "reason": "越界"},
            {"risk_score": True, "reason": "错误类型"},
            {"risk_score": "80", "reason": "错误类型"},
            {"risk_score": float("nan"), "reason": "非有限分数"},
            {"risk_score": 80, "reason": " "},
            {"risk_score": 80, "reason": "说明", "extra": "PRIVATE_RESPONSE_BODY"},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.respond(payload=payload)
                with self.assertRaises(CheckError) as caught:
                    deepseek_api.DeepSeekAPI().check("测试消息")
                self.assertNotIn("PRIVATE_RESPONSE_BODY", str(caught.exception))
                self.assertNotIn(FAKE_KEY, str(caught.exception))

    def test_think_wrapper_and_markdown_do_not_replace_final_json(self):
        self.respond(content='<think>internal notes {"risk_score": 5}</think>```json\n'
            '{"risk_score": 78, "reason": "包含强迫转发措辞"}\n```')
        self.assertEqual(deepseek_api.DeepSeekAPI().check("立即转发"), (78, "包含强迫转发措辞"))

    def test_missing_final_content_does_not_use_reasoning_content(self):
        body = json.dumps({"choices": [{"message": {
            "content": None,
            "reasoning_content": '{"risk_score": 99, "reason": "不应当作最终输出"}',
        }}]}).encode("utf-8")
        self.opener.open.return_value.__enter__.return_value.read.return_value = body
        with self.assertRaises(CheckError):
            deepseek_api.DeepSeekAPI().check("消息")

    def test_http_and_timeout_errors_do_not_expose_credentials_or_raw_body(self):
        sensitive = "PRIVATE_RESPONSE_BODY " + FAKE_KEY
        failures = [
            HTTPError("https://api.deepseek.com/chat/completions", 401, sensitive, {}, io.BytesIO(sensitive.encode())),
            HTTPError("https://api.deepseek.com/chat/completions", 429, sensitive, {}, io.BytesIO(sensitive.encode())),
            TimeoutError(sensitive),
            URLError(sensitive),
        ]
        for failure in failures:
            with self.subTest(failure_type=type(failure).__name__, code=getattr(failure, "code", None)):
                self.opener.open.reset_mock()
                self.opener.open.side_effect = failure
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    with self.assertRaises(CheckError) as caught:
                        deepseek_api.DeepSeekAPI().check("测试消息")
                self.assertTrue(str(caught.exception))
                self.assertNotIn(FAKE_KEY, str(caught.exception) + stderr.getvalue())
                self.assertNotIn("PRIVATE_RESPONSE_BODY", str(caught.exception) + stderr.getvalue())
                self.opener.open.assert_called_once()

    def prepare_factory(self, key):
        self.stack.enter_context(patch.dict(os.environ, {"DEEPSEEK_API_KEY": key}))
        self.stack.enter_context(patch.multiple(checkmodel, _instances={}, _available={}, _loaded=False))
        for module_name in checkmodel.MODEL_MODULES:
            model_class = checkmodel._load_module_class(module_name)
            if model_class is deepseek_api.DeepSeekAPI:
                continue
            self.stack.enter_context(patch.object(model_class, "detect", return_value=model_class is Ollama_DeepSeek))
            self.stack.enter_context(patch.object(model_class, "initialize"))

    def test_factory_prefers_one_cloud_instance_and_keeps_dynamic_name_in_results(self):
        self.prepare_factory(FAKE_KEY)
        models = checkmodel.get_models()
        matches = [model for model in models if model["id"] == "deepseek_r1"]
        self.assertEqual(len(matches), 1)
        instance = checkmodel.get_model("deepseek_r1")
        self.assertIsInstance(instance, deepseek_api.DeepSeekAPI)
        self.assertEqual(matches[0]["display_name"], instance.display_name)
        self.assertIn("deepseek-flash", instance.display_name.lower())
        risk_model = next(model for model in get_risk_models() if model["id"] == "deepseek_r1")
        self.assertEqual(risk_model["display_name"], instance.display_name)
        self.assertTrue(risk_model["available"])
        self.respond()
        result = run_risk_check("传播风险测试", ["deepseek_r1"], mode="single")
        self.assertEqual(result["members"][0]["display_name"], instance.display_name)
        self.assertEqual(result["members"][0]["score"], 72)

    def test_factory_without_key_falls_back_to_one_local_deepseek(self):
        self.prepare_factory("")
        models = checkmodel.get_models()
        matches = [model for model in models if model["id"] == "deepseek_r1"]
        self.assertEqual(len(matches), 1)
        instance = checkmodel.get_model("deepseek_r1")
        self.assertIsInstance(instance, Ollama_DeepSeek)
        self.assertEqual(matches[0]["display_name"], instance.display_name)
        self.opener.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
