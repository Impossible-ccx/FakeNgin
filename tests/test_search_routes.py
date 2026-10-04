"""The search page uses main's body-only BM25 with temporary storage."""

from contextlib import ExitStack
from html import unescape
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import checkmodel
from flask import template_rendered
from markupsafe import escape
from webapp import bm25, create_app, db, newsdata
from webapp.routes import search as search_routes


class SearchRouteTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.database_dir = Path(self.stack.enter_context(TemporaryDirectory()))
        self.stack.enter_context(patch.multiple(
            db, DATABASE_DIR=self.database_dir,
            USERS_FILE=self.database_dir / "users.csv", SESSIONS_FILE=self.database_dir / "sessions.csv",
        ))
        self.stack.enter_context(patch.object(newsdata, "NEWSDATA_DIR", self.database_dir / "newsdata"))
        index_dir = self.database_dir / "searchindex"
        self.stack.enter_context(patch.multiple(bm25, SEARCH_INDEX_DIR=index_dir,
                                               GLOBAL_META_FILE=index_dir / "meta.csv",
                                               GLOBAL_TERMS_FILE=index_dir / "terms.csv"))
        bm25.clear_cache()
        search_routes.clear_cache()
        self.addCleanup(bm25.clear_cache)
        self.addCleanup(search_routes.clear_cache)
        self.get_models = self.stack.enter_context(patch.object(checkmodel, "get_models", return_value=[]))
        self.get_model = self.stack.enter_context(patch.object(checkmodel, "get_model"))
        self.app = create_app()
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()
        self.contexts = []
        template_rendered.connect(self.capture_template, self.app, weak=False)
        self.addCleanup(template_rendered.disconnect, self.capture_template, self.app)

    def capture_template(self, sender, template, context, **extra):
        self.contexts.append((template.name, context))

    def seed(self, content, **values):
        newsdata.append_message({"content": content, "nature": newsdata.DEFAULT_NATURE, **values})
        return newsdata.load_all().iloc[-1].to_dict()

    def view(self, query=None, page=None, status=200):
        parameters = {}
        if query is not None:
            parameters["q"] = query
        if page is not None:
            parameters["page"] = page
        response = self.client.get("/search", query_string=parameters)
        self.assertEqual(response.status_code, status)
        self.assertEqual(self.contexts[-1][0], "search.html")
        self.get_model.assert_not_called()
        self.get_models.assert_not_called()
        return response, self.contexts[-1][1]

    def message_snapshot(self):
        # BM25 deliberately writes derived indexes; source message CSVs stay unchanged.
        return {path.name: path.read_bytes() for path in newsdata.NEWSDATA_DIR.glob("*.csv")}

    def test_empty_query_does_not_build_index_or_display_custom_word_cloud(self):
        self.seed("苹果 苹果")
        before = self.message_snapshot()
        with patch.object(bm25, "ranked_references") as ranked:
            response, context = self.view("  \t ")
        ranked.assert_not_called()
        self.assertEqual((context["query"], context["total"], context["total_matches"], context["rows"]), ("", 1, 0, []))
        self.assertNotIn("keywords", context)
        html = response.get_data(as_text=True)
        self.assertNotIn("keyword-cloud", html)
        self.assertNotIn('name="mode"', html)
        self.assertEqual(self.message_snapshot(), before)

    def test_only_body_tokens_match_and_unicode_uses_main_lower_rules(self):
        self.seed("今日 NASA 发布新闻", source="机构甲")
        self.seed("正文不含关键字", source="NaSa 公告")
        self.seed("Straße 天气信息")
        self.seed("无关文本", nature="真实", risk_reason="NASA 背景参考")
        _, context = self.view("  nasa  ")
        self.assertEqual((context["query"], context["total"], context["total_matches"]), ("nasa", 4, 1))
        self.assertEqual([row["content"] for row in context["rows"]], ["今日 NASA 发布新闻"])
        _, context = self.view("STRASSE")
        self.assertEqual(context["total_matches"], 0)
        _, context = self.view("Straße")
        self.assertEqual(context["rows"][0]["content"], "Straße 天气信息")
        _, context = self.view("uniquenonexistentterm")
        self.assertEqual((context["rows"], context["total_matches"]), ([], 0))

    def test_punctuation_does_not_become_regex_or_wildcard_search(self):
        self.seed("测试 正文 abc")
        self.seed("其他消息 xyz")
        for query in (".*", "%", "_", "' --"):
            with self.subTest(query=query):
                _, context = self.view(query)
                self.assertEqual(context["total_matches"], 0)
        _, context = self.view("[测试]")
        self.assertEqual(context["total_matches"], 1)
        self.assertEqual(context["rows"][0]["content"], "测试 正文 abc")

    def test_match_pagination_and_original_dataset_page_are_independent(self):
        for index in range(21):
            self.seed("unrelated {}".format(index))
        for index in range(41):
            self.seed("uniquematch {}".format(index))
        response, first = self.view("uniquematch")
        self.assertEqual((first["total"], first["total_matches"], first["total_pages"]), (62, 41, 3))
        self.assertEqual(len(first["rows"]), 20)
        self.assertEqual((first["rows"][0]["dataset_page"], first["rows"][0]["dataset_anchor"]), (2, "dataset-row-21"))
        self.assertIn("page=2", response.get_data(as_text=True))
        _, second = self.view("uniquematch", page=2)
        self.assertEqual((len(second["rows"]), second["rows"][0]["content"]), (20, "uniquematch 20"))
        _, last = self.view("uniquematch", page=9999)
        self.assertEqual((last["page"], len(last["rows"])), (3, 1))
        for page in (-1, 0, "bad", "1.5"):
            with self.subTest(page=page):
                _, context = self.view("uniquematch", page=page)
                self.assertEqual(context["page"], 1)

    def test_query_limit_rejects_before_reading_data_or_building_index(self):
        self.seed("字" * 200)
        _, context = self.view("字" * 200)
        self.assertEqual(context["total_matches"], 1)
        before = self.message_snapshot()
        with patch.object(newsdata, "load_all") as load, patch.object(bm25, "ranked_references") as ranked:
            _, context = self.view("字" * 201, status=400)
        self.assertIn("200", context["error"])
        load.assert_not_called()
        ranked.assert_not_called()
        self.assertEqual(self.message_snapshot(), before)

    def test_source_link_uses_global_index_across_multiple_csv_files(self):
        row = self.seed("uniquetarget 消息")
        prefix = newsdata.read_table(row["_file"]).iloc[[0] * 20].copy()
        prefix["content"] = "unrelated 前置样本"
        newsdata._write_path(newsdata.NEWSDATA_DIR / "a-prefix.csv", prefix)
        response, context = self.view("uniquetarget")
        match = context["rows"][0]
        self.assertEqual((match["_file"], match["_row"], match["dataset_page"], match["dataset_anchor"]),
                         (row["_file"], 0, 2, "dataset-row-20"))
        self.assertIn("/data?page=2#dataset-row-20", response.get_data(as_text=True))
        dataset_html = self.client.get("/data?page=2").get_data(as_text=True)
        self.assertIn('id="dataset-row-20"', dataset_html)

    def test_query_content_and_source_are_escaped_and_messages_stay_unchanged(self):
        attack = '<script>alert("SEARCH_XSS")</script>'
        self.seed(attack, source='<img src=x onerror="SEARCH_XSS">', nature="真实", fake_probability="91", risk_score="0")
        before = self.message_snapshot()
        response, context = self.view(attack)
        html = response.get_data(as_text=True)
        self.assertIn(str(escape(attack)), html)
        self.assertIn(str(escape('<img src=x onerror="SEARCH_XSS">')), html)
        self.assertNotIn(attack, html)
        self.assertNotIn('<img src=x onerror="SEARCH_XSS">', html)
        self.assertEqual((context["rows"][0]["nature"], context["rows"][0]["fake_probability"], context["rows"][0]["risk_score"]), ("真实", "91", "0"))
        self.assertEqual(self.message_snapshot(), before)

    def test_manual_link_preserves_exact_target_and_requires_login(self):
        row = self.seed("苹果 苹果", source="课程数据集")
        response, context = self.view("苹果")
        self.assertEqual(context["total_matches"], 1)
        urls = [unescape(value) for value in re.findall(r'href="([^"]+)"', response.get_data(as_text=True))]
        manual_url = next(url for url in urls if urlsplit(url).path == "/verify" and "signature=" in url)
        self.assertEqual(parse_qs(urlsplit(manual_url).query), {"file": [row["_file"]], "row": [str(row["_row"])], "signature": [row["_signature"]]})
        target = self.client.get(manual_url)
        self.assertEqual(target.status_code, 302)
        self.assertIn("/login", target.headers["Location"])

    def test_storage_failure_returns_clear_error_without_exception_details(self):
        with patch.object(newsdata, "load_all", side_effect=RuntimeError("PRIVATE_DATA_PATH")):
            response, context = self.view("关键字", status=503)
        self.assertIn("稍后重试", context["error"])
        self.assertNotIn("PRIVATE_DATA_PATH", response.get_data(as_text=True))

    def test_legacy_data_keyword_urls_redirect_to_independent_search(self):
        query = "NASA 新闻&%"
        with patch.object(newsdata, "load_all") as load:
            response = self.client.get("/data", query_string={"q": "  " + query + "  ", "page": 2})
        self.assertEqual(response.status_code, 302)
        target = urlsplit(response.headers["Location"])
        self.assertEqual((target.path, parse_qs(target.query)), ("/search", {"q": [query]}))
        load.assert_not_called()
        html = self.client.get("/data").get_data(as_text=True)
        self.assertIn('href="/search"', html)
        self.assertNotIn('name="q"', html)

    def test_page_uses_bm25_order_even_when_old_mode_argument_is_supplied(self):
        self.seed("苹果 香蕉 苹果")
        self.seed("香蕉 苹果")
        self.seed("无关消息", source="苹果 香蕉")
        expected = bm25.ranked_references("苹果 香蕉")
        _, context = self.view("苹果 香蕉")
        self.assertEqual([(row["_file"], row["_row"]) for row in context["rows"]], expected)
        self.assertEqual(context["total_matches"], 2)
        response = self.client.get("/search", query_string={"q": "苹果 香蕉", "mode":"literal"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.contexts[-1][1]["total_matches"], 2)
        self.assertNotIn("原文匹配", response.get_data(as_text=True))

    def test_search_cache_refreshes_body_labels_and_file_changes_without_indexing_source(self):
        row = self.seed("oldterm unique", source="provider_alpha")
        _, context = self.view("oldterm")
        self.assertEqual(context["total_matches"], 1)
        with patch.object(bm25, "_build_file_index", side_effect=AssertionError("warm cache rebuilt")):
            _, warm = self.view("oldterm")
        self.assertEqual(warm["total_matches"], 1)
        newsdata.update_message(row["_file"], row["_row"], row["_signature"], {"nature": "真实"})
        with patch.object(bm25, "_build_file_index", side_effect=AssertionError("label edit rebuilt tokens")):
            _, changed = self.view("oldterm")
        self.assertEqual(changed["rows"][0]["nature"], "真实")
        current = newsdata.load_all().iloc[0]
        newsdata.update_message(current["_file"], int(current["_row"]), current["_signature"],
                                {"content": "newterm unique", "source": "provider_beta"})
        _, old = self.view("oldterm")
        self.assertEqual(old["total_matches"], 0)
        _, source_only = self.view("provider_beta")
        self.assertEqual(source_only["total_matches"], 0)
        _, new = self.view("newterm")
        self.assertEqual(new["rows"][0]["source"], "provider_beta")
        self.seed("newterm imported")
        _, after_import = self.view("newterm")
        self.assertEqual(after_import["total_matches"], 2)
        current = newsdata.load_all().iloc[0]
        newsdata.delete_message(current["_file"], int(current["_row"]), current["_signature"])
        _, after_delete = self.view("newterm")
        self.assertEqual(after_delete["total_matches"], 1)


if __name__ == '__main__':
    unittest.main()
