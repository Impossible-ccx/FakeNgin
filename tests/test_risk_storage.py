"""确保风险批处理不覆盖旧概率、原始标签和已有标注。"""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import newscheck
from webapp import newsdata


class RiskStorageTests(unittest.TestCase):
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
        self.risk_model = Mock(name="risk_model")
        self.risk_model.score_kind = "risk"
        self.risk_model.name = "qwen2.5_7b"
        self.risk_model.prompt_version = "risk-v1"
        self.risk_model.check.return_value = (82, "措辞带有强迫转发特征")

    def test_risk_results_go_to_separate_fields_on_legacy_csv(self):
        self.assertEqual(newscheck.check_table(self.risk_model, "legacy.csv", False), (1, 0))
        row = newsdata.read_table("legacy.csv").iloc[0]
        self.assertEqual(row.fake_probability, "12")
        self.assertEqual(row.nature, "真实")
        self.assertEqual(row.risk_score, "82.00")
        self.assertEqual(row.risk_model, "qwen2.5_7b")
        self.assertEqual(row.risk_prompt_version, "risk-v1")
        self.assertEqual(row.risk_reason, "措辞带有强迫转发特征")
        self.assertEqual(newscheck.check_table(self.risk_model, "legacy.csv", False), (0, 0))

    def test_classifier_does_not_overwrite_existing_risk_fields(self):
        newscheck.check_table(self.risk_model, "legacy.csv", False)
        classifier = Mock(score_kind="probability")
        classifier.check.return_value = (31, "分类器输出")
        self.assertEqual(newscheck.check_table(classifier, "legacy.csv", True), (1, 0))
        row = newsdata.read_table("legacy.csv").iloc[0]
        self.assertEqual(row.fake_probability, "31.00")
        self.assertEqual(row.risk_score, "82.00")

    def test_existing_edit_form_preserves_risk_metadata(self):
        newscheck.check_table(self.risk_model, "legacy.csv", False)
        row = newsdata.read_table("legacy.csv").iloc[0]
        form = row.drop(labels=list(newsdata.RISK_COLUMNS)).to_dict()
        form["source"] = "更新来源"
        newsdata.update_message("legacy.csv", 0, newsdata.signature(row), newsdata.normalize_row(form))
        saved = newsdata.read_table("legacy.csv").iloc[0]
        self.assertEqual(saved.source, "更新来源")
        self.assertEqual(saved.risk_score, "82.00")
        self.assertEqual(saved.risk_reason, row.risk_reason)

    def test_invalid_score_does_not_become_a_stored_vote(self):
        self.risk_model.check.return_value = (float("nan"), "无效结果")
        self.assertEqual(newscheck.check_table(self.risk_model, "legacy.csv", False), (0, 1))
        row = newsdata.read_table("legacy.csv").iloc[0]
        self.assertEqual(row.risk_score, "")
        self.assertEqual(row.fake_probability, "12")

    def test_content_edit_invalidates_old_risk_and_allows_rescoring(self):
        newscheck.check_table(self.risk_model, "legacy.csv", False)
        row = newsdata.read_table("legacy.csv").iloc[0]
        newsdata.update_message("legacy.csv", 0, newsdata.signature(row), {"content": "另一条消息"})
        saved = newsdata.read_table("legacy.csv").iloc[0]
        for column in newsdata.RISK_COLUMNS:
            self.assertEqual(saved[column], "")
        self.assertEqual(newscheck.check_table(self.risk_model, "legacy.csv", False), (1, 0))


if __name__ == "__main__":
    unittest.main()
