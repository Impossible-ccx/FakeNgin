"""Dataset keyword counts come from actual bounded samples and invalidate on edits."""

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from webapp import keywords


class DatasetKeywordTests(unittest.TestCase):
    def setUp(self):
        keywords._count_keywords.cache_clear()
        self.addCleanup(keywords._count_keywords.cache_clear)

    def test_real_chinese_and_ascii_words_have_actual_occurrence_counts(self):
        cloud, sample_count = keywords.dataset_keywords(["苹果 苹果 NASA", "苹果 梨子 nasa"])
        counts = {item["text"]: item["count"] for item in cloud}
        self.assertEqual(sample_count, 2)
        self.assertEqual(counts["苹果"], 3)
        self.assertEqual(counts["nasa"], 2)
        self.assertEqual(counts["梨子"], 1)
        self.assertEqual(cloud[0]["text"], "苹果")
        self.assertTrue(all(1 <= item["weight"] <= 5 for item in cloud))

    def test_blank_punctuation_common_words_and_numbers_do_not_become_topics(self):
        with patch.object(keywords.jieba, "cut_for_search", side_effect=lambda text: text.split()):
            cloud, count = keywords.dataset_keywords(["的 这个 the 12345 !!! 食品安全", ""])
        self.assertEqual(count, 2)
        self.assertEqual([(item["text"], item["count"]) for item in cloud], [("食品安全", 1)])
        self.assertEqual(keywords.dataset_keywords([]), ([], 0))

    def test_sample_limit_does_not_consume_or_count_later_messages(self):
        messages = iter(["苹果"] * 500 + ["后续消息"])
        with patch.object(keywords.jieba, "cut_for_search", side_effect=lambda text: text.split()):
            cloud, count = keywords.dataset_keywords(messages)
        self.assertEqual((count, cloud[0]["count"]), (500, 500))
        self.assertEqual(next(messages), "后续消息")
        self.assertNotIn("后续消息", [item["text"] for item in cloud])

    def test_cache_reuses_only_unchanged_texts_and_returns_independent_results(self):
        with patch.object(keywords.jieba, "cut_for_search", side_effect=lambda text: text.split()) as tokenize:
            first, _ = keywords.dataset_keywords(["苹果 苹果"])
            first[0]["count"] = 999
            second, _ = keywords.dataset_keywords(["苹果 苹果"])
            self.assertEqual(second[0]["count"], 2)
            self.assertEqual(tokenize.call_count, 1)
            edited, _ = keywords.dataset_keywords(["梨子"])
            self.assertEqual(edited[0]["text"], "梨子")
            self.assertEqual(tokenize.call_count, 2)

    def test_cloud_limits_distinct_words_without_inventing_counts(self):
        words = ["主题{:02d}".format(index) for index in range(40)]
        with patch.object(keywords.jieba, "cut_for_search", side_effect=lambda text: text.split()):
            cloud, count = keywords.dataset_keywords([" ".join(words)])
        self.assertEqual(count, 1)
        self.assertEqual(len(cloud), 24)
        self.assertTrue(all(item["count"] == 1 for item in cloud))


if __name__ == '__main__':
    unittest.main()
