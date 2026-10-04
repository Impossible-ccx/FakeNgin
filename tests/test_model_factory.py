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
from checkmodel.ollama_qwen25 import Ollama_Qwen25
from checkmodel.ollama_deepseek import Ollama_DeepSeek
from webapp.models import list_cached_web_models, refresh_web_models


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


if __name__ == "__main__":
    unittest.main()
