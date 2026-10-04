"""模型加载和服务探测回归测试；不读取权重、不连接实际服务。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
import sys
from threading import Event
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import checkmodel
from checkmodel.base import CheckModel
from checkmodel import ollama_base
from checkmodel.ensemble import get_risk_models, run_risk_check
from checkmodel.ollama_qwen25 import Ollama_Qwen25
from checkmodel.ollama_deepseek import Ollama_DeepSeek
from webapp.models import list_cached_web_models, model_label, refresh_web_models


class ModelFactoryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.now = [100.0]
        self.stack.enter_context(patch.object(checkmodel, "monotonic", side_effect=lambda: self.now[0]))
        self.stack.enter_context(patch.object(checkmodel, "_states", {}))
        self.stack.enter_context(patch.object(checkmodel, "_module_errors", {}))
        self.stack.enter_context(patch.object(checkmodel, "_instances", {}))
        self.classes = {}
        for module, model_id, kind in [
            ("risk1", "qwen2.5_7b", "risk"),
            ("risk2", "deepseek_r1", "risk"),
            ("classifier", "roberta_rumor", "probability"),
        ]:
            self.classes[module] = type(module, (CheckModel,), {
                "name": model_id, "display_name": model_id, "description": "模型说明",
                "score_kind": kind, "detect": Mock(return_value=True), "initialize": Mock(),
            })
        self.stack.enter_context(patch.object(checkmodel, "MODEL_MODULES", list(self.classes)))
        self.loader = self.stack.enter_context(patch.object(
            checkmodel, "_load_module_class", side_effect=self.classes.__getitem__))

    def advance(self):
        self.now[0] += checkmodel.PROBE_CACHE_SECONDS + 0.1

    def test_cached_metadata_does_not_probe_or_construct_models(self):
        models = checkmodel.get_cached_models()
        self.assertEqual([item["available"] for item in models], [None] * 3)
        for model in self.classes.values():
            model.detect.assert_not_called()
            model.initialize.assert_not_called()
        self.assertTrue(all(state.instance is None for state in checkmodel._states.values()))

    def test_registration_is_available_independently_of_connection_or_initialization(self):
        registered = checkmodel.get_registered_models(score_kind="risk")
        self.assertEqual([model["id"] for model in registered], ["qwen2.5_7b", "deepseek_r1"])
        self.assertTrue(all("available" not in model for model in registered))
        for model in self.classes.values():
            model.detect.assert_not_called()
            model.initialize.assert_not_called()
        self.classes["risk1"].detect.return_value = False
        self.assertEqual([model["id"] for model in get_risk_models()], ["deepseek_r1"])
        self.assertEqual(checkmodel.get_registered_models(score_kind="risk"), registered)
        self.classes["classifier"].detect.assert_not_called()

    def register_more_risk_models(self):
        for index in (3, 4):
            module = "portable_risk_{}".format(index)
            self.classes[module] = type(module, (CheckModel,), {
                "name": module, "display_name": "适配器 {}".format(index),
                "description": "额外的风险适配器", "score_kind": "risk",
                "detect": Mock(return_value=True), "initialize": Mock(),
                "check": Mock(return_value=(80, "测试风险说明")),
            })
            checkmodel.MODEL_MODULES.append(module)
        for module in ("risk1", "risk2"):
            self.classes[module].check = Mock(return_value=(80, "测试风险说明"))

    def test_fourth_registered_adapter_is_a_candidate_and_can_be_used_without_other_rosters(self):
        self.register_more_risk_models()
        models = get_risk_models()
        self.assertEqual(len(models), 4)
        self.assertEqual(models[-1]["id"], "portable_risk_4")
        self.assertEqual(model_label("portable_risk_4"), "分析模型 4")
        result = run_risk_check("课程消息", ["portable_risk_4"], "single")
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["members"][0]["display_name"], "适配器 4")
        self.classes["portable_risk_4"].initialize.assert_called_once_with()
        self.classes["risk1"].initialize.assert_not_called()
        self.classes["classifier"].detect.assert_not_called()

    def test_registered_offline_fourth_adapter_remains_valid_and_recovers_after_ttl(self):
        self.register_more_risk_models()
        fourth = self.classes["portable_risk_4"]
        fourth.detect.return_value = False
        available = get_risk_models()
        self.assertEqual(len(available), 3)
        self.assertEqual(model_label("portable_risk_4"), "分析模型 4")
        result = run_risk_check("课程消息", ["qwen2.5_7b", "deepseek_r1", "portable_risk_4"])
        self.assertEqual(result["members"][-1]["status"], "unavailable")
        self.assertEqual((result["selected_count"], result["success_count"]), (3, 2))
        fourth.initialize.assert_not_called()
        fourth.detect.return_value = True
        self.advance()
        recovered = run_risk_check("课程消息", ["portable_risk_4"], "single")
        self.assertEqual(recovered["members"][0]["status"], "ok")
        self.assertEqual(model_label("portable_risk_4"), "分析模型 4")

    def test_initialized_adapter_can_go_offline_and_recover_without_reinitializing(self):
        self.register_more_risk_models()
        first = checkmodel.get_model("portable_risk_4")
        fourth = self.classes["portable_risk_4"]
        fourth.detect.return_value = False
        self.now[0] += 0.1
        self.assertEqual(checkmodel.get_models(model_ids=["portable_risk_4"], refresh=True), [])
        offline = run_risk_check("课程消息", ["portable_risk_4"], "single")
        self.assertEqual(offline["members"][0]["status"], "unavailable")
        self.assertIs(checkmodel._instances["portable_risk_4"], first)
        fourth.detect.return_value = True
        self.advance()
        self.assertIs(checkmodel.get_model("portable_risk_4"), first)
        fourth.initialize.assert_called_once_with()

    def test_unknown_adapter_and_probability_model_are_rejected_before_lookup(self):
        self.register_more_risk_models()
        with patch.object(checkmodel, "get_model") as lookup:
            for model_id in ("unknown_adapter", "roberta_rumor"):
                with self.subTest(model_id=model_id), self.assertRaises(ValueError):
                    run_risk_check("课程消息", [model_id], "single")
            with self.assertRaises(ValueError):
                run_risk_check("课程消息", [model["id"] for model in checkmodel.get_registered_models(score_kind="risk")])
            lookup.assert_not_called()

    def test_web_labels_follow_registration_order_and_removed_report_models_use_index(self):
        self.register_more_risk_models()
        self.classes["risk1"].detect.return_value = False
        models = refresh_web_models()
        self.assertEqual([model["display_name"] for model in models], ["分析模型 2", "分析模型 3", "分析模型 4"])
        self.assertEqual(model_label({"id": "unregistered_historical_adapter"}, 2), "分析模型 2")
        self.assertEqual(model_label({"id": "unregistered_historical_adapter"}), "分析模型")

    def test_listing_probes_without_initializing_any_weights(self):
        self.assertEqual(len(checkmodel.get_models()), 3)
        self.assertEqual(len(checkmodel.get_models()), 3)
        for model in self.classes.values():
            model.detect.assert_called_once_with()
            model.initialize.assert_not_called()

    def test_filtered_listing_does_not_probe_the_classifier(self):
        self.assertEqual(len(checkmodel.get_models(model_ids=["qwen2.5_7b"])), 1)
        self.classes["classifier"].detect.assert_not_called()
        self.classes["classifier"].initialize.assert_not_called()
        self.classes["risk2"].detect.assert_not_called()

    def test_only_selected_model_initializes_and_instance_is_reused(self):
        first = checkmodel.get_model("qwen2.5_7b")
        self.assertIs(checkmodel.get_model("qwen2.5_7b"), first)
        self.classes["risk1"].initialize.assert_called_once_with()
        for module in ("risk2", "classifier"):
            self.classes[module].detect.assert_not_called()
            self.classes[module].initialize.assert_not_called()

    def test_failed_probe_recovers_after_short_ttl_without_restart(self):
        self.classes["risk1"].detect.return_value = False
        self.assertEqual(checkmodel.get_models(model_ids=["qwen2.5_7b"]), [])
        self.classes["risk1"].detect.return_value = True
        self.assertEqual(checkmodel.get_models(model_ids=["qwen2.5_7b"]), [])
        with self.assertRaises(KeyError):
            checkmodel.get_model("qwen2.5_7b")
        self.advance()
        model = checkmodel.get_model("qwen2.5_7b")
        self.assertEqual(model.name, "qwen2.5_7b")
        self.assertEqual(self.classes["risk1"].detect.call_count, 2)

    def test_explicit_refresh_recovers_before_ttl_and_retains_initialized_instances(self):
        self.classes["risk1"].detect.return_value = False
        self.assertEqual(checkmodel.get_models(model_ids=["qwen2.5_7b"]), [])
        self.classes["risk1"].detect.return_value = True
        self.now[0] += 0.1
        self.assertEqual(len(checkmodel.get_models(model_ids=["qwen2.5_7b"], refresh=True)), 1)
        first = checkmodel.get_model("qwen2.5_7b")
        self.now[0] += 0.1
        self.classes["risk1"].detect.return_value = False
        self.assertEqual(checkmodel.get_models(model_ids=["qwen2.5_7b"], refresh=True), [])
        with self.assertRaises(KeyError):
            checkmodel.get_model("qwen2.5_7b")
        self.classes["risk1"].detect.return_value = True
        self.advance()
        self.assertIs(checkmodel.get_model("qwen2.5_7b"), first)
        self.classes["risk1"].initialize.assert_called_once_with()

    def test_initialization_failure_is_cached_then_retried_with_a_clean_instance(self):
        self.classes["risk1"].initialize.side_effect = RuntimeError("PRIVATE_LOADING_DETAILS")
        with self.assertLogs("checkmodel", level="WARNING") as logs:
            with self.assertRaises(KeyError):
                checkmodel.get_model("qwen2.5_7b")
        self.assertNotIn("PRIVATE_LOADING_DETAILS", " ".join(logs.output))
        self.classes["risk1"].initialize.side_effect = None
        with self.assertRaises(KeyError):
            checkmodel.get_model("qwen2.5_7b")
        self.classes["risk1"].initialize.assert_called_once_with()
        self.advance()
        self.assertEqual(checkmodel.get_model("qwen2.5_7b").name, "qwen2.5_7b")
        self.assertEqual(self.classes["risk1"].initialize.call_count, 2)

    def test_concurrent_first_requests_initialize_once(self):
        entered, release = Event(), Event()

        def initialize():
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test synchronization timeout")

        self.classes["risk1"].initialize.side_effect = initialize
        with ThreadPoolExecutor(max_workers=6) as workers:
            first = workers.submit(checkmodel.get_model, "qwen2.5_7b")
            try:
                self.assertTrue(entered.wait(3))
                others = [workers.submit(checkmodel.get_model, "qwen2.5_7b") for _ in range(5)]
            finally:
                release.set()
            instances = [future.result(timeout=3) for future in [first] + others]
        self.assertTrue(all(instance is instances[0] for instance in instances))
        self.classes["risk1"].detect.assert_called_once_with()
        self.classes["risk1"].initialize.assert_called_once_with()

    def test_concurrent_probe_shares_result_and_cached_metadata_does_not_wait(self):
        entered, release = Event(), Event()

        def detect():
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test synchronization timeout")
            return True

        self.classes["risk1"].detect.side_effect = detect
        with ThreadPoolExecutor(max_workers=6) as workers:
            first = workers.submit(checkmodel.get_models, ["qwen2.5_7b"])
            try:
                self.assertTrue(entered.wait(3))
                # 正在探测的线程持有实例锁，此调用必须立即取得旧快照。
                self.assertIsNone(checkmodel.get_cached_models(["qwen2.5_7b"])[0]["available"])
                others = [workers.submit(checkmodel.get_models, ["qwen2.5_7b"]) for _ in range(5)]
            finally:
                release.set()
            self.assertTrue(all(len(future.result(timeout=3)) == 1 for future in [first] + others))
        self.classes["risk1"].detect.assert_called_once_with()
        self.classes["risk1"].initialize.assert_not_called()

    def test_unknown_model_does_not_probe_or_load_other_models(self):
        with self.assertRaises(KeyError):
            checkmodel.get_model("missing")
        for model in self.classes.values():
            model.detect.assert_not_called()
            model.initialize.assert_not_called()

    def test_module_registration_failure_can_be_refreshed(self):
        self.stack.enter_context(patch.object(checkmodel, "MODEL_MODULES", ["risk1"]))
        self.loader.side_effect = ImportError("optional dependency missing")
        with self.assertLogs("checkmodel", level="WARNING"):
            self.assertEqual(checkmodel.get_models(), [])
        self.loader.side_effect = self.classes.__getitem__
        self.assertEqual(checkmodel.get_models(), [])
        self.now[0] += 0.1
        self.assertEqual(len(checkmodel.get_models(refresh=True)), 1)
        self.classes["risk1"].initialize.assert_not_called()

    def test_web_cached_metadata_and_refresh_only_use_risk_models(self):
        models = list_cached_web_models()
        self.assertEqual(len(models), 2)
        self.assertTrue(all(model["needs_probe"] for model in models))
        self.assertTrue(all(model["display_name"].startswith("分析模型") for model in models))
        self.classes["risk2"].detect.return_value = False
        models = refresh_web_models()
        self.assertEqual([model["id"] for model in models], ["qwen2.5_7b"])
        self.assertFalse(models[0]["needs_probe"])
        self.assertEqual([model["id"] for model in list_cached_web_models()], ["qwen2.5_7b"])
        self.classes["classifier"].detect.assert_not_called()
        for model in self.classes.values():
            model.initialize.assert_not_called()


class SharedOllamaProbeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.now = [100.0]
        self.stack.enter_context(patch.object(ollama_base, "monotonic", side_effect=lambda: self.now[0]))
        self.stack.enter_context(patch.object(ollama_base, "_probe_names", None))
        self.stack.enter_context(patch.object(ollama_base, "_probe_checked_at", None))
        self.client = Mock()
        self.client.list.return_value = {"models": [{"name": "qwen2.5:7b"}, {"model": "deepseek-r1:7b"}]}
        self.ollama = types.SimpleNamespace(Client=Mock(return_value=self.client))
        self.stack.enter_context(patch.dict(sys.modules, {"ollama": self.ollama}))

    def test_models_share_one_short_timeout_probe_and_keep_inference_timeout(self):
        self.assertTrue(Ollama_Qwen25().detect())
        self.assertTrue(Ollama_DeepSeek().detect())
        self.client.list.assert_called_once_with()
        self.ollama.Client.assert_called_once_with(timeout=2)
        Ollama_DeepSeek()._client(self.ollama)
        self.assertEqual(self.ollama.Client.call_args.kwargs["timeout"], 120)

    def test_probe_failure_recovers_after_ttl_and_by_explicit_refresh(self):
        self.client.list.side_effect = RuntimeError("service offline")
        self.assertFalse(Ollama_Qwen25().detect())
        self.client.list.side_effect = None
        self.assertFalse(Ollama_Qwen25().detect())
        self.now[0] += ollama_base.PROBE_CACHE_SECONDS + 0.1
        self.assertTrue(Ollama_Qwen25().detect())
        self.now[0] += 0.1
        self.client.list.return_value = {"models": []}
        self.assertFalse(Ollama_Qwen25().refresh_detection(self.now[0]))
        self.client.list.return_value = {"models": [{"name": "deepseek-r1:7b"}]}
        self.now[0] += 0.1
        self.assertTrue(Ollama_DeepSeek().refresh_detection(self.now[0]))

    def test_same_refresh_request_shares_the_installed_model_list(self):
        self.assertTrue(Ollama_Qwen25().detect())
        self.now[0] += 0.1
        requested_at = self.now[0]
        self.assertTrue(Ollama_Qwen25().refresh_detection(requested_at))
        self.assertTrue(Ollama_DeepSeek().refresh_detection(requested_at))
        self.assertEqual(self.client.list.call_count, 2)

    def test_custom_client_probe_preserves_host_auth_and_inference_timeout(self):
        class AuthenticatedAdapter(Ollama_Qwen25):
            model_name = "custom-risk:7b"
            timeout = 77

            def _client(self, sdk):
                return sdk.Client(host="http://custom-service.test:11434", timeout=self.timeout,
                                  headers={"Authorization": "test-token"})

        custom_client = Mock()
        custom_client.list.return_value = {"models": [{"model": "custom-risk:7b"}]}
        self.ollama.Client.side_effect = lambda **kwargs: custom_client if "host" in kwargs else self.client
        self.assertTrue(Ollama_Qwen25().detect())
        adapter = AuthenticatedAdapter()
        self.assertTrue(adapter.detect())
        self.assertTrue(adapter.detect())
        self.ollama.Client.assert_called_with(host="http://custom-service.test:11434", timeout=77,
                                             headers={"Authorization": "test-token"})
        custom_client.list.assert_called_once_with()
        self.client.list.assert_called_once_with()
        self.assertEqual(adapter.timeout, 77)
        adapter._client(self.ollama)
        self.assertEqual(self.ollama.Client.call_args.kwargs["timeout"], 77)

    def test_custom_clients_have_separate_caches_and_recover_by_ttl_or_refresh(self):
        class EndpointAdapter(Ollama_Qwen25):
            def __init__(self, host):
                self.host = host

            def _client(self, sdk):
                return sdk.Client(host=self.host, timeout=self.timeout)

        left_client, right_client = Mock(), Mock()
        left_client.list.return_value = {"models": [{"model": "qwen2.5:7b"}]}
        right_client.list.side_effect = RuntimeError("isolated offline test service")
        clients = {"http://left.test:11434": left_client, "http://right.test:11434": right_client}
        self.ollama.Client.side_effect = lambda **kwargs: clients[kwargs["host"]] if "host" in kwargs else self.client
        left, right = EndpointAdapter("http://left.test:11434"), EndpointAdapter("http://right.test:11434")
        self.assertTrue(left.detect())
        self.assertFalse(right.detect())
        right_client.list.side_effect = None
        right_client.list.return_value = {"models": [{"model": "qwen2.5:7b"}]}
        self.assertFalse(right.detect())
        self.assertTrue(left.detect())
        self.assertTrue(Ollama_DeepSeek().detect())
        self.assertEqual((left_client.list.call_count, right_client.list.call_count, self.client.list.call_count), (1, 1, 1))
        self.now[0] += ollama_base.PROBE_CACHE_SECONDS + 0.1
        self.assertTrue(right.detect())
        right_client.list.return_value = {"models": []}
        self.now[0] += 0.1
        self.assertFalse(right.refresh_detection(self.now[0]))
        self.assertTrue(left.detect())
        self.assertEqual(right_client.list.call_count, 3)

    def test_concurrent_custom_client_probes_share_only_that_adapter_result(self):
        class CustomAdapter(Ollama_Qwen25):
            def _client(self, sdk):
                return sdk.Client(host="http://custom-service.test:11434", timeout=self.timeout)

        entered, release = Event(), Event()

        def list_models():
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test synchronization timeout")
            return {"models": [{"model": "qwen2.5:7b"}]}

        self.client.list.side_effect = list_models
        adapter = CustomAdapter()
        with ThreadPoolExecutor(max_workers=6) as workers:
            first = workers.submit(adapter.detect)
            try:
                self.assertTrue(entered.wait(3))
                others = [workers.submit(adapter.detect) for _ in range(5)]
            finally:
                release.set()
            self.assertTrue(all(future.result(timeout=3) for future in [first] + others))
        self.client.list.assert_called_once_with()
        self.ollama.Client.assert_called_once_with(host="http://custom-service.test:11434", timeout=60)


if __name__ == "__main__":
    unittest.main()
