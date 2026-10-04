"""确保批处理写入 fake_probability，且不覆盖已有分数、原始标签与标注。"""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import newscheck
from webapp import newsdata


class FakeProbabilityStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.patch = patch.object(newsdata, "NEWSDATA_DIR", self.directory)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        pd.DataFrame([{
            "content": "一条消息", "nature": "真实", "fake_probability": "12",
            "source": "原始来源", "publish_time": "", "process_time": "",
        }]).to_csv(self.directory / "legacy.csv", index=False)
        self.model = Mock(name="model")
        self.model.name = "qwen2.5_7b"
        self.model.check.return_value = (82, "措辞带有强迫转发特征")

    def test_existing_probability_is_preserved_without_override(self):
        self.assertEqual(newscheck.check_table(self.model, "legacy.csv", False), (0, 0))
        row = newsdata.read_table("legacy.csv").iloc[0]
        self.assertEqual(row.fake_probability, "12")
        self.assertEqual(row.nature, "真实")
        self.assertNotIn("risk_score", row.index)

    def test_override_writes_fake_probability(self):
        self.assertEqual(newscheck.check_table(self.model, "legacy.csv", True), (1, 0))
        row = newsdata.read_table("legacy.csv").iloc[0]
        self.assertEqual(row.fake_probability, "82.00")
        self.assertEqual(row.nature, "真实")

    def test_invalid_score_does_not_overwrite(self):
        self.model.check.return_value = (float("nan"), "无效结果")
        self.assertEqual(newscheck.check_table(self.model, "legacy.csv", True), (0, 1))
        row = newsdata.read_table("legacy.csv").iloc[0]
        self.assertEqual(row.fake_probability, "12")

    def test_edit_form_round_trip_preserves_probability(self):
        newscheck.check_table(self.model, "legacy.csv", True)
        row = newsdata.read_table("legacy.csv").iloc[0]
        form = row.to_dict()
        form["source"] = "更新来源"
        newsdata.update_message(
            "legacy.csv", 0, newsdata.signature(row), newsdata.normalize_row(form))
        saved = newsdata.read_table("legacy.csv").iloc[0]
        self.assertEqual(saved.source, "更新来源")
        self.assertEqual(saved.fake_probability, "82.00")

    def test_content_edit_keeps_existing_probability(self):
        newscheck.check_table(self.model, "legacy.csv", True)
        row = newsdata.read_table("legacy.csv").iloc[0]
        newsdata.update_message("legacy.csv", 0, newsdata.signature(row), {"content": "另一条消息"})
        saved = newsdata.read_table("legacy.csv").iloc[0]
        self.assertEqual(saved.content, "另一条消息")
        self.assertEqual(saved.fake_probability, "82.00")


if __name__ == "__main__":
    unittest.main()
