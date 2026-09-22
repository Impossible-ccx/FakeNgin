"""远程模型协议与错误处理回归测试，不访问真实接口。"""

import io
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from checkmodel.base import CheckError
from checkmodel.compatible_api import CompatibleAPIModel


class CompatibleAPIModelTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {
            "MODEL_API_BASE_URL": "http://model.test:3333/v1/",
            "MODEL_API_KEY": "test-secret",
            "MODEL_API_MODEL": "test-model",
            "MODEL_API_TIMEOUT": "12",
        }, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.model = CompatibleAPIModel()

    def response(self, content):
        return io.BytesIO(json.dumps({
            "choices": [{"message": {"content": content}}],
        }).encode())

    @patch("checkmodel.compatible_api.urlopen")
    def test_request_and_valid_response(self, open_url):
        open_url.return_value = self.response('{"probability": 42, "reason": "证据不足"}')
        self.assertEqual(self.model.check("测试消息"), (42.0, "证据不足"))
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "http://model.test:3333/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-secret")
        self.assertEqual(request.get_method(), "POST")
        body = json.loads(request.data)
        self.assertEqual(body["model"], "test-model")
        self.assertEqual(body["messages"][-1]["content"], "测试消息")
        self.assertEqual(open_url.call_args.kwargs["timeout"], 12)

    @patch("checkmodel.compatible_api.urlopen")
    def test_truncated_output_raises_clear_error(self, open_url):
        open_url.return_value = io.BytesIO(json.dumps({
            "choices": [{"message": {"content": '{"probability": 78, "reason": "未完'},
                         "finish_reason": "length"}],
        }).encode())
        with self.assertRaises(CheckError) as ctx:
            self.model.check("测试消息")
        self.assertIn("MODEL_API_MAX_TOKENS", str(ctx.exception))

    @patch("checkmodel.compatible_api.urlopen")
    def test_fenced_json(self, open_url):
        open_url.return_value = self.response('```json\n{"probability": 0, "reason": "测试"}\n```')
        self.assertEqual(self.model.check("测试"), (0.0, "测试"))

    @patch("checkmodel.compatible_api.urlopen")
    def test_invalid_model_output(self, open_url):
        invalid = [
            'not JSON', '{}', '[]',
            '{"probability": -1, "reason": "测试"}',
            '{"probability": 101, "reason": "测试"}',
            '{"probability": NaN, "reason": "测试"}',
            '{"probability": true, "reason": "测试"}',
            '{"probability": "50", "reason": "测试"}',
            '{"probability": 50, "reason": ""}',
        ]
        for content in invalid:
            with self.subTest(content=content):
                open_url.return_value = self.response(content)
                with self.assertRaises(CheckError):
                    self.model.check("测试")

    @patch("checkmodel.compatible_api.urlopen")
    def test_transport_failure_does_not_echo_credentials(self, open_url):
        failures = [
            HTTPError("http://model.test", 401, "test-secret", {}, None),
            URLError("test-secret"), TimeoutError("test-secret"),
        ]
        for error in failures:
            with self.subTest(error=type(error).__name__):
                open_url.side_effect = error
                with self.assertRaises(CheckError) as caught:
                    self.model.check("测试")
                self.assertNotIn("test-secret", str(caught.exception))

    @patch("checkmodel.compatible_api.urlopen")
    def test_missing_configuration_never_sends_request(self, open_url):
        for key in ("MODEL_API_BASE_URL", "MODEL_API_KEY", "MODEL_API_MODEL"):
            with self.subTest(key=key), patch.dict(os.environ, {key: ""}):
                model = CompatibleAPIModel()
                self.assertFalse(model.detect())
                with self.assertRaises(CheckError):
                    model.check("测试")
        open_url.assert_not_called()

    def test_invalid_timeout(self):
        for timeout in ("0", "-1", "nan", "inf", "invalid"):
            with self.subTest(timeout=timeout), patch.dict(os.environ, {"MODEL_API_TIMEOUT": timeout}):
                with self.assertRaises(ValueError):
                    CompatibleAPIModel()


if __name__ == "__main__":
    unittest.main()

    @patch("checkmodel.compatible_api.urlopen")
    def test_request_timeout_raises_checkerror(self, open_url):
        """模型服务无响应（超时）：抛出可理解的 CheckError，不悬挂。"""
        import socket
        open_url.side_effect = socket.timeout("timed out")
        with self.assertRaises(CheckError) as ctx:
            self.model.check("测试消息")
        self.assertIn("超时", str(ctx.exception))
        # 网络类失败不在错误消息中回显密钥
        self.assertNotIn("test-secret", str(ctx.exception))
