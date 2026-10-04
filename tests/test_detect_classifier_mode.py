"""风险检测页「分类器检测」模式测试；模型与数据全部 mock。"""

from contextlib import ExitStack
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import checkmodel
from webapp import create_app, db, newsdata


CLASSIFIER = {
    "id": "roberta_rumor",
    "display_name": "中文 RoBERTa 谣言分类器",
    "description": "本地微调分类模型",
    "score_kind": "probability",
    "available": True,
}
RISK_MODEL = {
    "id": "qwen2.5_7b",
    "display_name": "Qwen2.5-7B (Ollama)",
    "description": "风险模型",
    "score_kind": "risk",
    "available": True,
}


class ClassifierModeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        database_dir = Path(self.stack.enter_context(TemporaryDirectory()))
        self.stack.enter_context(patch.multiple(
            db,
            DATABASE_DIR=database_dir,
            USERS_FILE=database_dir / "users.csv",
            SESSIONS_FILE=database_dir / "sessions.csv",
        ))
        self.stack.enter_context(patch.object(newsdata, "NEWSDATA_DIR", database_dir / "newsdata"))
        self.stack.enter_context(patch.object(
            checkmodel, "get_registered_models", return_value=[CLASSIFIER, RISK_MODEL],
        ))
        self.model = Mock(check=Mock(return_value=(87.6, "分类器输出：谣言 88%")))
        self.stack.enter_context(patch.object(checkmodel, "get_model", return_value=self.model))
        self.app = create_app()
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()

    def test_detect_page_offers_classifier_mode(self):
        response = self.client.get("/detect")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("分类器检测", html)
        self.assertIn(CLASSIFIER["display_name"], html)

    def test_detect_page_classifier_mode_returns_probability(self):
        response = self.client.post("/detect", data={
            "mode": "classifier", "message": "测试消息", "classifier_model": "roberta_rumor",
        })
        html = response.get_data(as_text=True)
        self.assertIn("88%", html)
        self.assertIn("真假分类检测", html)
        self.model.check.assert_called_once_with("测试消息")

    def test_detect_page_classifier_mode_requires_valid_selection(self):
        response = self.client.post("/detect", data={
            "mode": "classifier", "message": "测试消息", "classifier_model": "qwen2.5_7b",
        })
        self.assertIn("请选择可用的分类模型", response.get_data(as_text=True))
        self.model.check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
