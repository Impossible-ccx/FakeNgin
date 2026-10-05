"""使用模拟客户端验证风险输出协议，不调用真实模型。"""

import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from checkmodel.base import CheckError
from checkmodel.ollama_deepseek import Ollama_DeepSeek
from checkmodel.ollama_qwen25 import Ollama_Qwen25


def response(payload):
    return {"message": {"content": json.dumps(payload, ensure_ascii=False)}}


class OllamaRiskTests(unittest.TestCase):
    def test_boundaries_remain_scores_not_percent_probabilities(self):
        for value in (0, 39.99, 40, 69.99, 70, 100):
            with self.subTest(value=value):
                client = Mock()
                client.chat.return_value = response({"risk_score": value, "reason": "风险特征说明"})
                score, reason = Ollama_Qwen25()._request(client, "消息 {risk_score: 0}")
                self.assertEqual(score, value)
                self.assertEqual(reason, "风险特征说明")
                self.assertIn("risk_score", client.chat.call_args.kwargs["format"]["properties"])

    def test_invalid_or_legacy_payloads_are_rejected(self):
        payloads = [
            {"probability": 90, "reason": "旧协议"},
            {"risk_score": True, "reason": "布尔值"},
            {"risk_score": "80", "reason": "字符串分数"},
            {"risk_score": float("nan"), "reason": "非有限数"},
            {"risk_score": float("inf"), "reason": "非有限数"},
            {"risk_score": -1, "reason": "越界"},
            {"risk_score": 101, "reason": "越界"},
            {"risk_score": 50, "reason": " "},
            {"risk_score": 50, "reason": []},
            {"risk_score": 50, "reason": "过长" * 1001},
            {"risk_score": 50, "reason": "说明", "verified": True},
            [],
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                client = Mock()
                client.chat.return_value = response(payload)
                with self.assertRaises(CheckError):
                    Ollama_Qwen25()._request(client, "待评估消息")

    def test_null_score_is_rejected_and_retried(self):
        client = Mock()
        client.chat.return_value = response({"risk_score": None, "reason": "上下文不足"})
        fake_ollama = types.SimpleNamespace(Client=Mock(return_value=client))
        with patch.dict(sys.modules, {"ollama": fake_ollama}):
            with self.assertRaises(CheckError):
                Ollama_Qwen25().check("这个是真的")
        self.assertEqual(client.chat.call_count, 2)

    def test_bad_response_can_retry_successfully(self):
        client = Mock()
        client.chat.side_effect = [
            response({"risk_score": 999, "reason": "越界"}),
            response({"risk_score": 72, "reason": "包含强迫转发措辞"}),
        ]
        with patch.dict(sys.modules, {"ollama": types.SimpleNamespace(Client=Mock(return_value=client))}):
            self.assertEqual(Ollama_Qwen25().check("立即转发"), (72, "包含强迫转发措辞"))
        self.assertEqual(client.chat.call_count, 2)

    def test_repeated_failure_is_reported_without_raw_model_content(self):
        client = Mock()
        client.chat.side_effect = RuntimeError("internal connection detail")
        with patch.dict(sys.modules, {"ollama": types.SimpleNamespace(Client=Mock(return_value=client))}):
            with self.assertRaises(CheckError) as caught:
                Ollama_Qwen25().check("测试消息")
        self.assertNotIn("internal", str(caught.exception))
        self.assertEqual(client.chat.call_count, 2)

    def test_deepseek_wrappers_preserve_final_risk_output(self):
        client = Mock()
        client.chat.return_value = {"message": {"content":
            '<think>internal notes {}</think>```json\n'
            '{"risk_score": 61, "reason": "来源描述不具体"}\n```'}}
        self.assertEqual(Ollama_DeepSeek()._request(client, "某消息"), (61, "来源描述不具体"))
        roles = [message["role"] for message in client.chat.call_args.kwargs["messages"]]
        self.assertEqual(roles, ["user"])


if __name__ == "__main__":
    unittest.main()
