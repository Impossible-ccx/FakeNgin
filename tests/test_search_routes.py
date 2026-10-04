"""Literal dataset search and keyword navigation use temporary storage only."""

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

    def view(self, query=None, page=None, status=200, mode="literal"):
        parameters = {"mode": mode}
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

    def storage_snapshot(self):
        return {str(path.relative_to(self.database_dir)): path.read_bytes() for path in self.database_dir.rglob("*") if path.is_file()}

    def test_empty_query_shows_actual_keywords_without_unrelated_results(self):
        _, context = self.view()
        self.assertEqual((context["total"], context["total_matches"], context["rows"]), (0, 0, []))
        self.seed("苹果 苹果")
        before = self.storage_snapshot()
        response, context = self.view("  \t ")
        self.assertEqual(context["query"], "")
        self.assertEqual((context["total"], context["keyword_sample_count"]), (1, 1))
        self.assertEqual((context["rows"], context["total_matches"]), ([], 0))
        self.assertIn({"text": "苹果", "count": 2, "weight": 3}, context["keywords"])
        self.assertIn("数据集关键词", response.get_data(as_text=True))
        self.assertEqual(self.storage_snapshot(), before)

    def test_content_and_source_substrings_match_casefolded_literal_text(self):
        self.seed("今日 NASA 发布新闻", source="机构甲")
        self.seed("正文不含关键字", source="NaSa 公告")
        self.seed("Straße 天气信息")
        self.seed("无关文本", nature="真实", risk_reason="NASA 背景参考")
        _, context = self.view("  nasa  ")
        self.assertEqual((context["query"], context["total"], context["total_matches"]), ("nasa", 4, 2))
        self.assertEqual([row["content"] for row in context["rows"]], ["今日 NASA 发布新闻", "正文不含关键字"])
        _, context = self.view("STRASSE")
        self.assertEqual(context["total_matches"], 1)
        _, context = self.view("不存在的关键字")
        self.assertEqual((context["rows"], context["total_matches"]), ([], 0))

    def test_regex_and_sql_characters_do_not_broaden_literal_matching(self):
        self.seed("特定值 a.b [甲] 100% _name ' OR 1=1 --")
        self.seed("axb 1000 任意文本")
        for query in ("a.b", "[甲]", "100%", "_name", "' OR 1=1 --"):
            with self.subTest(query=query):
                _, context = self.view(query)
                self.assertEqual(context["total_matches"], 1)
        _, context = self.view(".*")
        self.assertEqual(context["total_matches"], 0)

    def test_match_pagination_and_original_dataset_page_are_independent(self):
        for index in range(21):
            self.seed("无关前置消息 {}".format(index))
        for index in range(41):
            self.seed("分页关键字 {}".format(index))
        response, first = self.view("分页关键字")
        self.assertEqual((first["total"], first["total_matches"], first["total_pages"]), (62, 41, 3))
        self.assertEqual(len(first["rows"]), 20)
        self.assertEqual((first["rows"][0]["dataset_page"], first["rows"][0]["dataset_anchor"]), (2, "dataset-row-21"))
        self.assertIn("page=2", response.get_data(as_text=True))
        _, second = self.view("分页关键字", page=2)
        self.assertEqual((len(second["rows"]), second["rows"][0]["content"]), (20, "分页关键字 20"))
        _, last = self.view("分页关键字", page=9999)
        self.assertEqual((last["page"], len(last["rows"])), (3, 1))
        for page in (-1, 0, "bad", "1.5"):
            with self.subTest(page=page):
                _, context = self.view("分页关键字", page=page)
                self.assertEqual(context["page"], 1)

    def test_query_limit_rejects_before_reading_data_or_computing_keywords(self):
        self.seed("字" * 200)
        _, context = self.view("字" * 200)
        self.assertEqual(context["total_matches"], 1)
        before = self.storage_snapshot()
        with patch.object(newsdata, "load_all") as load:
            _, context = self.view("字" * 201, status=400)
        self.assertIn("200", context["error"])
        load.assert_not_called()
        self.assertEqual(self.storage_snapshot(), before)

    def test_source_link_uses_global_index_across_multiple_csv_files(self):
        row = self.seed("目标文件消息")
        prefix = newsdata.read_table(row["_file"]).iloc[[0] * 20].copy()
        prefix["content"] = "另一个文件的前置样本"
        newsdata._write_path(newsdata.NEWSDATA_DIR / "a-prefix.csv", prefix)
        response, context = self.view("目标文件")
        match = context["rows"][0]
        self.assertEqual((match["_file"], match["_row"], match["dataset_page"], match["dataset_anchor"]),
                         (row["_file"], 0, 2, "dataset-row-20"))
        self.assertIn("/data?page=2#dataset-row-20", response.get_data(as_text=True))
        dataset_html = self.client.get("/data?page=2").get_data(as_text=True)
        self.assertIn('id="dataset-row-20"', dataset_html)

    def test_query_content_source_and_keyword_links_are_escaped_and_read_only(self):
        attack = '<script>alert("SEARCH_XSS")</script>'
        self.seed(attack, source='<img src=x onerror="SEARCH_XSS">', nature="真实", fake_probability="91", risk_score="0")
        before = self.storage_snapshot()
        response, context = self.view(attack)
        html = response.get_data(as_text=True)
        self.assertIn(str(escape(attack)), html)
        self.assertIn(str(escape('<img src=x onerror="SEARCH_XSS">')), html)
        self.assertNotIn(attack, html)
        self.assertNotIn('<img src=x onerror="SEARCH_XSS">', html)
        self.assertEqual((context["rows"][0]["nature"], context["rows"][0]["fake_probability"], context["rows"][0]["risk_score"]), ("真实", "91", "0"))
        self.assertEqual(self.storage_snapshot(), before)

    def test_keyword_cloud_links_and_manual_links_preserve_exact_targets(self):
        row = self.seed("苹果 苹果", source="课程数据集")
        response, _ = self.view("苹果")
        urls = [unescape(value) for value in re.findall(r'href="([^"]+)"', response.get_data(as_text=True))]
        keyword_url = next(url for url in urls if urlsplit(url).path == "/search" and parse_qs(urlsplit(url).query).get("q") == ["苹果"])
        _, context = self.view(parse_qs(urlsplit(keyword_url).query)["q"][0])
        self.assertEqual(context["total_matches"], 1)
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

    def test_default_search_uses_related_ranking_and_literal_mode_keeps_phrase_matching(self):
        self.seed("苹果 香蕉 苹果")
        self.seed("香蕉 苹果")
        self.seed("无关消息", source="苹果 香蕉")
        expected = bm25.ranked_references("苹果 香蕉")
        response = self.client.get("/search", query_string={"q": "苹果 香蕉"})
        self.assertEqual(response.status_code, 200)
        context = self.contexts[-1][1]
        self.assertEqual(context["mode"], "related")
        self.assertEqual([(row["_file"], row["_row"]) for row in context["rows"]], expected)
        self.assertEqual(context["total_matches"], 3)
        self.assertIn('value="related" selected', response.get_data(as_text=True))
        _, literal = self.view("苹果 香蕉")
        self.assertEqual(literal["total_matches"], 2)
        self.get_model.assert_not_called()

    def test_search_cache_is_invalidated_by_content_source_labels_and_file_set_changes(self):
        row = self.seed("旧词 独有", source="机构甲")
        _, context = self.view("旧词", mode="related")
        self.assertEqual(context["total_matches"], 1)
        with patch.object(bm25, "_build_file_index", side_effect=AssertionError("warm cache rebuilt")):
            _, warm = self.view("旧词", mode="related")
        self.assertEqual(warm["total_matches"], 1)
        newsdata.update_message(row["_file"], row["_row"], row["_signature"], {"nature": "真实"})
        with patch.object(bm25, "_build_file_index", side_effect=AssertionError("label edit rebuilt tokens")):
            _, changed = self.view("旧词", mode="related")
        self.assertEqual(changed["rows"][0]["nature"], "真实")
        current = newsdata.load_all().iloc[0]
        newsdata.update_message(current["_file"], int(current["_row"]), current["_signature"],
                                {"content": "新词 独有", "source": "机构乙"})
        _, old = self.view("旧词", mode="related")
        self.assertEqual(old["total_matches"], 0)
        _, new = self.view("机构乙", mode="related")
        self.assertEqual(new["total_matches"], 1)
        self.seed("新词 导入消息")
        _, after_import = self.view("新词", mode="related")
        self.assertEqual(after_import["total_matches"], 2)
        current = newsdata.load_all().iloc[0]
        newsdata.delete_message(current["_file"], int(current["_row"]), current["_signature"])
        _, after_delete = self.view("新词", mode="related")
        self.assertEqual(after_delete["total_matches"], 1)

    def test_related_pagination_preserves_mode_and_invalid_mode_is_rejected(self):
        for index in range(21):
            self.seed("分页 关键词 {}".format(index))
        response, context = self.view("关键词", mode="related")
        self.assertEqual((context["total_matches"], len(context["rows"]), context["total_pages"]), (21, 20, 2))
        self.assertIn("mode=related", response.get_data(as_text=True))
        _, last = self.view("关键词", page=2, mode="related")
        self.assertEqual(len(last["rows"]), 1)
        with patch.object(newsdata, "load_all") as load:
            self.view("关键词", mode="invalid", status=400)
        load.assert_not_called()


if __name__ == '__main__':
    unittest.main()
