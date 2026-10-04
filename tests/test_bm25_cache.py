"""BM25 cache tests use temporary CSV datasets and indexes, never real storage."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from webapp import bm25, newsdata


class BM25CacheTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(TemporaryDirectory()))
        self.data_dir = self.directory / "newsdata"
        self.index_dir = self.directory / "searchindex"
        self.data_dir.mkdir()
        self.stack.enter_context(patch.object(newsdata, "NEWSDATA_DIR", self.data_dir))
        self.stack.enter_context(patch.multiple(
            bm25, SEARCH_INDEX_DIR=self.index_dir,
            GLOBAL_META_FILE=self.index_dir / "meta.csv",
            GLOBAL_TERMS_FILE=self.index_dir / "terms.csv",
        ))
        newsdata.clear_cache()
        bm25.clear_cache()
        self.addCleanup(newsdata.clear_cache)
        self.addCleanup(bm25.clear_cache)

    def seed(self, name="sample.csv", contents=("apple",), source="", nature="未校验"):
        rows = [{column: "" for column in newsdata.COLUMNS} for _ in contents]
        for row, content in zip(rows, contents):
            row.update(content=content, source=source, nature=nature)
        frame = pd.DataFrame(rows, columns=newsdata.COLUMNS)
        frame.to_csv(self.data_dir / name, index=False)
        return frame

    def split_words(self):
        return patch.object(bm25.jieba, "cut_for_search", side_effect=lambda text: str(text).split())

    def index_snapshot(self):
        return {
            path.relative_to(self.index_dir).as_posix(): (
                path.stat().st_mtime_ns, path.stat().st_ctime_ns,
                path.stat().st_ino, path.read_bytes(),
            )
            for path in self.index_dir.rglob("*.csv")
            if path.is_file()
        }

    def test_warm_queries_reuse_parsed_csv_document_tokens_and_disk_index(self):
        self.seed(contents=("apple apple", "banana"), source="provider")
        with self.split_words() as tokenize, \
                patch.object(newsdata.pd, "read_csv", wraps=pd.read_csv) as read:
            first = bm25.search("apple")
            reads_after_first = read.call_count
            tokens_after_first = tokenize.call_count
            disk_after_first = self.index_snapshot()
            repeated = bm25.search(" APPLE ")
            self.assertEqual((read.call_count, tokenize.call_count), (reads_after_first, tokens_after_first))
            other_query = bm25.search("banana")
            self.assertEqual(read.call_count, reads_after_first)
            self.assertEqual(tokenize.call_count, tokens_after_first + 1)
            self.assertEqual(tokenize.call_args.args[0], "banana")
            self.assertEqual(self.index_snapshot(), disk_after_first)
        self.assertEqual(first, repeated)
        self.assertEqual(other_query[0]["content"], "banana")

    def test_body_change_only_reindexes_affected_table(self):
        self.seed(contents=("apple",))
        self.seed("other.csv", ("banana",))
        with self.split_words() as tokenize:
            bm25.search("apple")
            original_index = self.index_snapshot()
            tokenize.reset_mock()
            self.seed(contents=("pear",))
            self.assertEqual(bm25.search("pear")[0]["content"], "pear")
            tokenized = [call.args[0] for call in tokenize.call_args_list]
            self.assertIn("pear ", tokenized)
            self.assertNotIn("banana ", tokenized)
            self.assertEqual(bm25.search("apple"), [])
            updated_index = self.index_snapshot()
        for path, value in original_index.items():
            if path.startswith("other.csv/"):
                self.assertEqual(updated_index[path], value)
        self.assertNotEqual(updated_index["sample.csv/postings.csv"], original_index["sample.csv/postings.csv"])

    def test_source_change_reindexes_affected_table_and_is_searchable(self):
        self.seed(contents=("unrelated content",), source="provider_alpha")
        self.seed("other.csv", ("stable document",), source="stable_provider")
        with self.split_words() as tokenize:
            self.assertEqual(bm25.search("provider_alpha")[0]["source"], "provider_alpha")
            baseline = self.index_snapshot()
            tokenize.reset_mock()
            table = newsdata.read_table("sample.csv")
            table.loc[0, "source"] = "provider_beta"
            newsdata.write_table("sample.csv", table)
            changed = bm25.search("provider_beta")
            self.assertEqual(changed[0]["source"], "provider_beta")
            self.assertEqual(bm25.search("provider_alpha"), [])
            texts = [call.args[0] for call in tokenize.call_args_list]
            self.assertIn("unrelated content provider_beta", texts)
            self.assertNotIn("stable document stable_provider", texts)
            current = self.index_snapshot()
        for path, value in baseline.items():
            if path.startswith("other.csv/"):
                self.assertEqual(current[path], value)

    def test_label_change_returns_new_record_without_document_retokenization(self):
        self.seed(contents=("apple",), nature="未校验")
        with self.split_words() as tokenize:
            initial = bm25.search("apple")[0]
            baseline = self.index_snapshot()
            tokenize.reset_mock()
            newsdata.update_message(initial["_file"], initial["_row"], initial["_signature"],
                                    {"nature": "真实", "fake_probability": "10.00"})
            updated = bm25.search("apple")[0]
            self.assertEqual((updated["nature"], updated["fake_probability"]), ("真实", "10.00"))
            self.assertNotEqual(updated["_signature"], initial["_signature"])
            tokenize.assert_not_called()
            current = self.index_snapshot()
        for name in ("sample.csv/postings.csv", "sample.csv/doc_lengths.csv", "sample.csv/terms.csv", "terms.csv"):
            self.assertEqual(current[name], baseline[name])

    def test_added_and_deleted_tables_invalidate_rankings_and_prune_index(self):
        self.seed(contents=("apple",))
        with self.split_words():
            self.assertEqual(bm25.ranked_references("apple"), [("sample.csv", 0)])
            self.seed("added.csv", ("apple",))
            self.assertEqual(set(bm25.ranked_references("apple")), {("sample.csv", 0), ("added.csv", 0)})
            (self.data_dir / "sample.csv").unlink()
            self.assertEqual(bm25.ranked_references("apple"), [("added.csv", 0)])
            self.assertFalse((self.index_dir / "sample.csv").exists())
            (self.data_dir / "added.csv").unlink()
            self.assertEqual(bm25.search("apple"), [])

    def test_same_mtime_and_size_atomic_replacement_invalidates_cached_query(self):
        self.seed(contents=("alpha",))
        with self.split_words():
            self.assertEqual(bm25.search("alpha")[0]["content"], "alpha")
            original = self.data_dir / "sample.csv"
            original_stat = original.stat()
            self.seed("replacement.tmp", ("bravo",))
            replacement = self.data_dir / "replacement.tmp"
            self.assertEqual(replacement.stat().st_size, original_stat.st_size)
            os.utime(replacement, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            replacement.replace(original)
            self.assertEqual(original.stat().st_mtime_ns, original_stat.st_mtime_ns)
            self.assertEqual(bm25.search("alpha"), [])
            self.assertEqual(bm25.search("bravo")[0]["content"], "bravo")

    def test_relevance_order_default_three_and_unlimited_references(self):
        self.seed(contents=("apple", "apple banana banana banana banana banana banana banana", "apple apple", "apple apple apple", "banana"))
        with self.split_words():
            references = bm25.ranked_references("apple")
            self.assertEqual(references, [("sample.csv", index) for index in (3, 2, 0, 1)])
            self.assertEqual([row["_row"] for row in bm25.search("apple")], [3, 2, 0])
            self.assertEqual([row["_row"] for row in bm25.search("apple", limit=100)], [3, 2, 0, 1])
            self.assertEqual(bm25.search("apple", limit=0), [])
            self.assertEqual(bm25.search("apple", limit=-1), [])

    def test_ranked_references_keep_all_matches_for_page_selection(self):
        self.seed(contents=tuple("apple {}".format(index) for index in range(45)))
        with self.split_words():
            references = bm25.ranked_references("apple")
            self.assertEqual(len(references), 45)
            self.assertEqual(references[20:40], [("sample.csv", index) for index in range(20, 40)])
            self.assertEqual(references[40:], [("sample.csv", index) for index in range(40, 45)])
            self.assertEqual(len(bm25.search("apple")), 3)

    def test_restart_reuses_disk_index_without_retokenizing_messages(self):
        self.seed(contents=("apple", "banana"))
        with self.split_words() as tokenize:
            initial = bm25.search("apple")
            baseline = self.index_snapshot()
            bm25.clear_cache()
            newsdata.clear_cache()
            tokenize.reset_mock()
            self.assertEqual(bm25.search("apple"), initial)
            self.assertEqual([call.args[0] for call in tokenize.call_args_list], ["apple"])
            self.assertEqual(self.index_snapshot(), baseline)

    def test_cache_isolated_between_dataset_and_index_directories(self):
        self.seed(contents=("apple",))
        alternate_data = self.directory / "alternate-newsdata"
        alternate_index = self.directory / "alternate-searchindex"
        alternate_data.mkdir()
        self.seed("../alternate-newsdata/sample.csv", ("banana",))
        with self.split_words():
            self.assertEqual(bm25.search("apple")[0]["content"], "apple")
            first_disk = self.index_snapshot()
            with patch.object(newsdata, "NEWSDATA_DIR", alternate_data), patch.multiple(
                bm25, SEARCH_INDEX_DIR=alternate_index,
                GLOBAL_META_FILE=alternate_index / "meta.csv",
                GLOBAL_TERMS_FILE=alternate_index / "terms.csv",
            ):
                self.assertEqual(bm25.search("apple"), [])
                self.assertEqual(bm25.search("banana")[0]["content"], "banana")
            self.assertEqual(bm25.search("apple")[0]["content"], "apple")
            self.assertEqual(bm25.search("banana"), [])
            self.assertEqual(self.index_snapshot(), first_disk)

    def test_concurrent_first_queries_build_once_and_return_independent_records(self):
        self.seed(contents=("apple apple", "banana"))
        start = Barrier(8)

        def browse(index):
            start.wait(timeout=10)
            rows = bm25.search("apple")
            original = rows[0]["content"]
            rows[0]["nature"] = str(index)
            return original

        with self.split_words() as tokenize:
            with ThreadPoolExecutor(max_workers=8) as pool:
                self.assertEqual(list(pool.map(browse, range(8))), ["apple apple"] * 8)
            texts = [call.args[0] for call in tokenize.call_args_list]
            self.assertEqual(texts.count("apple apple "), 1)
            self.assertEqual(texts.count("banana "), 1)
            self.assertEqual(bm25.search("apple")[0]["nature"], "未校验")

    def test_returned_reference_list_and_records_do_not_mutate_query_cache(self):
        self.seed(contents=("apple", "apple"))
        with self.split_words():
            references = bm25.ranked_references("apple")
            references.clear()
            records = bm25.search("apple")
            records[0]["content"] = "客户端临时编辑"
            records.pop()
            self.assertEqual(bm25.ranked_references("apple"), [("sample.csv", 0), ("sample.csv", 1)])
            self.assertEqual([row["content"] for row in bm25.search("apple")], ["apple", "apple"])

    def test_unicode_casefold_matches_real_jieba_segmentation(self):
        self.seed(contents=("Straße 科学", "NASA 科技"), source="机构甲")
        self.assertEqual(bm25.search("STRASSE")[0]["content"], "Straße 科学")
        self.assertEqual(bm25.search("nasa")[0]["content"], "NASA 科技")

    def test_partial_global_write_recovers_without_losing_new_messages(self):
        for target in (bm25.GLOBAL_META_FILE, bm25.GLOBAL_TERMS_FILE):
            with self.subTest(target=target.name), self.split_words():
                self.seed(contents=())
                self.assertEqual(bm25.search("apple"), [])
                self.seed(contents=("apple",))
                original_write = bm25._write_csv

                def fail_global_write(frame, path, columns):
                    if Path(path) == target:
                        raise OSError("simulated interrupted global write")
                    return original_write(frame, path, columns)

                with patch.object(bm25, "_write_csv", side_effect=fail_global_write):
                    with self.assertRaises(OSError):
                        bm25.search("apple")
                # 每表索引已写完，重试仍需更新全局统计，不能永久返回空结果。
                self.assertEqual(bm25.search("apple")[0]["content"], "apple")

    def test_missing_derived_index_files_rebuild_after_restart(self):
        self.seed(contents=("apple",))
        with self.split_words():
            self.assertEqual(bm25.search("apple")[0]["content"], "apple")
            missing_files = [self.index_dir / "sample.csv" / name
                             for name in ("postings.csv", "doc_lengths.csv", "terms.csv")]
            missing_files.append(bm25.GLOBAL_TERMS_FILE)
            for target in missing_files:
                with self.subTest(target=target):
                    target.unlink()
                    bm25.clear_cache()
                    self.assertEqual(bm25.search("apple")[0]["content"], "apple")
                    self.assertTrue(target.is_file())


if __name__ == "__main__":
    unittest.main()
