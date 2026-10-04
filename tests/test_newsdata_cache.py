"""CSV remains authoritative while unchanged browsing reuses memory snapshots."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from webapp import newsdata


class NewsdataCacheTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(TemporaryDirectory()))
        self.stack.enter_context(patch.object(newsdata, "NEWSDATA_DIR", self.directory))
        newsdata.clear_cache()
        self.addCleanup(newsdata.clear_cache)

    def seed(self, name="sample.csv", contents=("初始消息",), **values):
        frame = pd.DataFrame([
            {column: (content if column == "content" else values.get(column, ""))
             for column in newsdata.COLUMNS}
            for content in contents
        ], columns=newsdata.COLUMNS)
        frame.to_csv(self.directory / name, index=False)
        return frame

    def test_repeated_table_reads_parse_once_and_return_independent_frames(self):
        self.seed(nature="真实", fake_probability="20.00")
        with patch.object(newsdata.pd, "read_csv", wraps=pd.read_csv) as read:
            first = newsdata.read_table("sample.csv")
            first.loc[0, "content"] = "局部修改"
            first["extra"] = "临时列"
            second = newsdata.read_table("sample.csv")
            self.assertEqual(read.call_count, 1)
        self.assertEqual(second.loc[0, "content"], "初始消息")
        self.assertEqual(second.loc[0, "fake_probability"], "20.00")
        self.assertEqual(list(second.columns), newsdata.COLUMNS)

    def test_repeated_aggregate_reads_reuse_parsing_and_row_signatures(self):
        self.seed(contents=("一", "二"))
        self.seed("other.csv", ("三",))
        with patch.object(newsdata.pd, "read_csv", wraps=pd.read_csv) as read, \
                patch.object(newsdata, "_signature", wraps=newsdata._signature) as sign:
            first = newsdata.load_all()
            self.assertEqual((read.call_count, sign.call_count), (2, 3))
            first.loc[0, "_signature"] = "本地篡改"
            first.loc[0, "content"] = "本地修改"
            second = newsdata.load_all()
            self.assertEqual((read.call_count, sign.call_count), (2, 3))
        self.assertEqual(second["content"].tolist(), ["三", "一", "二"])
        self.assertEqual(second["_file"].tolist(), ["other.csv", "sample.csv", "sample.csv"])
        self.assertEqual(second["_row"].tolist(), [0, 0, 1])
        self.assertNotEqual(second.loc[0, "_signature"], "本地篡改")

    def test_updating_one_table_only_reparses_and_resigns_that_table(self):
        self.seed(contents=("一", "二"))
        self.seed("other.csv", ("三",))
        newsdata.load_all()
        with patch.object(newsdata.pd, "read_csv", wraps=pd.read_csv) as read, \
                patch.object(newsdata, "_signature", wraps=newsdata._signature) as sign:
            self.seed("other.csv", ("外部更新",))
            result = newsdata.load_all()
            self.assertEqual((read.call_count, sign.call_count), (1, 1))
        self.assertEqual(result["content"].tolist(), ["外部更新", "一", "二"])

    def test_append_update_and_delete_change_visible_snapshot_and_version(self):
        newsdata.append_message({"content": "人工添加", "nature": "未校验", "risk_score": "70"})
        first = newsdata.load_all().iloc[0]
        initial_version = newsdata.dataset_fingerprint()
        newsdata.update_message(first["_file"], first["_row"], first["_signature"],
                                {"content": "编辑原文", "nature": "真实"})
        changed = newsdata.load_all().iloc[0]
        self.assertNotEqual(newsdata.dataset_fingerprint(), initial_version)
        self.assertEqual((changed["content"], changed["nature"], changed["risk_score"]),
                         ("编辑原文", "真实", ""))
        self.assertNotEqual(changed["_signature"], first["_signature"])
        with self.assertRaisesRegex(ValueError, "已发生变化"):
            newsdata.update_message(first["_file"], first["_row"], first["_signature"],
                                    {"nature": "虚假"})
        newsdata.delete_message(changed["_file"], changed["_row"], changed["_signature"])
        self.assertTrue(newsdata.load_all().empty)
        newsdata.append_message({"content": "新添加"})
        self.assertEqual(newsdata.load_all()["content"].tolist(), ["新添加"])

    def test_bulk_write_table_refreshes_cache_without_reinterpreting_labels(self):
        self.seed(nature="中立", fake_probability="14.25")
        before = newsdata.load_all().iloc[0]
        table = newsdata.read_table("sample.csv")
        table.loc[0, "risk_score"] = "0"
        table.loc[0, "risk_reason"] = "批量检测结果"
        newsdata.write_table("sample.csv", table)
        saved = newsdata.load_all().iloc[0]
        self.assertEqual((saved["nature"], saved["fake_probability"], saved["risk_score"]),
                         ("中立", "14.25", "0"))
        self.assertNotEqual(saved["_signature"], before["_signature"])
        self.assertEqual(list(pd.read_csv(self.directory / "sample.csv").columns), newsdata.COLUMNS)

    def test_external_atomic_replacement_is_seen_with_same_size_and_mtime(self):
        self.seed(contents=("甲",))
        original = self.directory / "sample.csv"
        before_stat = original.stat()
        before = newsdata.load_all().iloc[0]
        before_version = newsdata.dataset_fingerprint()
        self.seed("replacement.tmp", ("乙",))
        self.assertEqual((self.directory / "replacement.tmp").stat().st_size, before_stat.st_size)
        os.utime(self.directory / "replacement.tmp", ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))
        (self.directory / "replacement.tmp").replace(original)
        self.assertEqual(original.stat().st_mtime_ns, before_stat.st_mtime_ns)
        self.assertNotEqual(newsdata.dataset_fingerprint(), before_version)
        after = newsdata.load_all().iloc[0]
        self.assertEqual(after["content"], "乙")
        self.assertNotEqual(after["_signature"], before["_signature"])

    def test_external_added_deleted_and_renamed_files_refresh_aggregate(self):
        self.seed()
        newsdata.load_all()
        initial = newsdata.dataset_fingerprint()
        self.seed("added.csv", ("新增文件消息",))
        self.assertNotEqual(newsdata.dataset_fingerprint(), initial)
        self.assertEqual(newsdata.load_all()["content"].tolist(), ["新增文件消息", "初始消息"])
        (self.directory / "sample.csv").unlink()
        (self.directory / "added.csv").replace(self.directory / "renamed.csv")
        result = newsdata.load_all()
        self.assertEqual(result["content"].tolist(), ["新增文件消息"])
        self.assertEqual(result["_file"].tolist(), ["renamed.csv"])
        (self.directory / "renamed.csv").unlink()
        self.assertEqual(list(newsdata.load_all().columns), newsdata.COLUMNS + newsdata.META_COLUMNS)
        self.assertTrue(newsdata.load_all().empty)

    def test_atomic_replacement_during_read_does_not_publish_old_content(self):
        self.seed(contents=("旧快照",))
        original_open = Path.open
        replaced = False

        @contextmanager
        def racing_open(path, *args, **kwargs):
            nonlocal replaced
            with original_open(path, *args, **kwargs) as source:
                yield source
            # Windows 不允许替换仍打开的文件。模拟关闭读取句柄后、
            # 发布缓存前，另一个进程恰好原子替换了同名 CSV。
            if path == self.directory / "sample.csv" and not replaced:
                replaced = True
                self.seed("replacement.tmp", ("新快照",))
                (self.directory / "replacement.tmp").replace(self.directory / "sample.csv")

        with patch.object(Path, "open", racing_open), \
                patch.object(newsdata.pd, "read_csv", wraps=pd.read_csv) as read:
            first = newsdata.load_all()
            second = newsdata.load_all()
        self.assertEqual(first["content"].tolist(), ["新快照"])
        self.assertEqual(second["content"].tolist(), ["新快照"])
        self.assertEqual(read.call_count, 2)

    def test_cache_isolated_between_dataset_directories_and_can_be_cleared(self):
        self.seed(contents=("目录一",))
        first_key = newsdata.dataset_fingerprint()
        newsdata.load_all()
        other = self.directory / "other-data"
        other.mkdir()
        self.seed("other-data/sample.csv", ("目录二",))
        with patch.object(newsdata, "NEWSDATA_DIR", other):
            self.assertNotEqual(newsdata.dataset_fingerprint(), first_key)
            self.assertEqual(newsdata.load_all()["content"].tolist(), ["目录二"])
        self.assertEqual(newsdata.load_all()["content"].tolist(), ["目录一"])
        with patch.object(newsdata.pd, "read_csv", wraps=pd.read_csv) as read:
            newsdata.clear_cache()
            self.assertEqual(newsdata.load_all()["content"].tolist(), ["目录一"])
            self.assertEqual(read.call_count, 1)

    def test_concurrent_first_reads_parse_once_and_keep_return_values_independent(self):
        self.seed()
        start = Barrier(8)

        def browse(index):
            start.wait(timeout=10)
            frame = newsdata.load_all()
            frame.loc[0, "content"] = str(index)
            return frame.loc[0, "content"]

        with patch.object(newsdata.pd, "read_csv", wraps=pd.read_csv) as read:
            with ThreadPoolExecutor(max_workers=8) as pool:
                self.assertEqual(list(pool.map(browse, range(8))), [str(index) for index in range(8)])
            self.assertEqual(read.call_count, 1)
        self.assertEqual(newsdata.load_all().loc[0, "content"], "初始消息")

    def test_concurrent_append_does_not_lose_messages(self):
        start = Barrier(6)

        def append(index):
            start.wait(timeout=10)
            newsdata.append_message({"content": "并发消息 {}".format(index)})

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(append, range(6)))
        self.assertEqual(set(newsdata.load_all()["content"]), {"并发消息 {}".format(index) for index in range(6)})
        self.assertFalse(list(self.directory.glob("*.tmp")))

    def test_many_tables_evict_old_snapshots_instead_of_growing_without_limit(self):
        self.seed("first.csv")
        newsdata.read_table("first.csv")
        for index in range(newsdata._TABLE_CACHE_LIMIT):
            name = "table-{}.csv".format(index)
            self.seed(name, (str(index),))
            newsdata.read_table(name)
        with patch.object(newsdata.pd, "read_csv", wraps=pd.read_csv) as read:
            self.assertEqual(newsdata.read_table("first.csv").loc[0, "content"], "初始消息")
            self.assertEqual(read.call_count, 1)


if __name__ == "__main__":
    unittest.main()
