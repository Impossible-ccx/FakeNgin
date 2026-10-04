"""Literal dataset-search integration tests using temporary storage only."""

from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from markupsafe import escape
from webapp import batches, newsdata, reports

import test_detect_routes as helpers


class SearchRouteTests(unittest.TestCase):
    setUp = helpers.DetectRouteTests.setUp
    capture_template = helpers.DetectRouteTests.capture_template
    lookup_sources = helpers.DetectRouteTests.lookup_sources
    lookup_model = helpers.DetectRouteTests.lookup_model
    database_snapshot = helpers.DetectRouteTests.database_snapshot

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
        return response, self.contexts[-1][1]

    def storage_snapshot(self):
        return {
            str(path.relative_to(self.database_dir)): path.read_bytes()
            for path in self.database_dir.rglob("*") if path.is_file()
        }

    def test_empty_query_does_not_list_or_infer_unrelated_rows(self):
        _, context = self.view()
        self.assertEqual((context["total"], context["total_matches"], context["rows"]), (0, 0, []))
        self.seed("已有数据，但还没有关键字")
        before = self.storage_snapshot()
        with patch.object(batches, "latest_reports") as latest:
            _, context = self.view("  \t ")
        self.assertEqual(context["query"], "")
        self.assertEqual(context["total"], 1)
        self.assertEqual(context["rows"], [])
        self.assertEqual(context["total_matches"], 0)
        latest.assert_not_called()
        self.assertEqual(self.storage_snapshot(), before)

    def test_content_and_source_substrings_are_casefolded_literal_matches(self):
        self.seed("今日 NASA 发布新闻", source="机构甲")
        self.seed("正文不含关键字", source="NaSa 公告")
        self.seed("Straße 天气信息")
        self.seed("无关文本", nature="真实", risk_reason="NASA 背景参考")
        _, context = self.view("  nasa  ")
        self.assertEqual(context["query"], "nasa")
        self.assertEqual(context["total"], 4)
        self.assertEqual(context["total_matches"], 2)
        self.assertEqual([row["content"] for row in context["rows"]], ["今日 NASA 发布新闻", "正文不含关键字"])
        _, context = self.view("STRASSE")
        self.assertEqual(context["total_matches"], 1)
        self.assertEqual(context["rows"][0]["content"], "Straße 天气信息")
        _, context = self.view("不存在的关键字")
        self.assertEqual(context["rows"], [])
        self.assertEqual(context["total_matches"], 0)

    def test_regex_and_sql_characters_do_not_broaden_literal_matching(self):
        self.seed("特定值 a.b [甲] 100% _name ' OR 1=1 --")
        self.seed("axb 1000 任意文本")
        for query in ("a.b", "[甲]", "100%", "_name", "' OR 1=1 --"):
            with self.subTest(query=query):
                _, context = self.view(query)
                self.assertEqual(context["total_matches"], 1)
                self.assertEqual(context["rows"][0]["_row"], 0)
        _, context = self.view(".*")
        self.assertEqual(context["total_matches"], 0)

    def test_match_pagination_and_original_dataset_page_are_independent(self):
        for index in range(41):
            self.seed("无关前置消息 {}".format(index))
        for index in range(41):
            self.seed("分页关键字 {}".format(index))
        _, first = self.view("分页关键字")
        self.assertEqual((first["total"], first["total_matches"], first["total_pages"]), (82, 41, 3))
        self.assertEqual(len(first["rows"]), 20)
        self.assertEqual(first["rows"][0]["dataset_page"], 3)
        self.assertEqual(first["rows"][0]["dataset_anchor"], "dataset-row-41")
        self.assertEqual(first["rows"][0]["_row"], 41)
        _, second = self.view("分页关键字", page=2)
        self.assertEqual(len(second["rows"]), 20)
        self.assertEqual(second["rows"][0]["content"], "分页关键字 20")
        self.assertEqual(second["rows"][0]["dataset_page"], 4)
        self.assertEqual(second["rows"][0]["dataset_anchor"], "dataset-row-61")
        _, last = self.view("分页关键字", page=9999)
        self.assertEqual((last["page"], len(last["rows"])), (3, 1))
        for page in (-1, 0, "bad", "1.5"):
            with self.subTest(page=page):
                _, context = self.view("分页关键字", page=page)
                self.assertEqual(context["page"], 1)
        _, context = self.view("未命中", page=999)
        self.assertEqual((context["page"], context["total_pages"]), (1, 1))

    def test_query_limit_rejects_before_any_dataset_read_or_report_lookup(self):
        self.seed("字" * 200)
        _, context = self.view("字" * 200)
        self.assertEqual(context["total_matches"], 1)
        before = self.storage_snapshot()
        with patch.object(newsdata, "load_all") as load, patch.object(batches, "latest_reports") as latest:
            _, context = self.view("字" * 201, status=400)
        self.assertIn("200", context["error"])
        self.assertEqual(context["rows"], [])
        load.assert_not_called()
        latest.assert_not_called()
        self.assertEqual(self.storage_snapshot(), before)

    def test_source_row_link_uses_global_index_across_multiple_csv_files(self):
        row = self.seed("目标文件消息")
        prefix = newsdata.read_table(row["_file"]).iloc[[0] * 20].copy()
        prefix["content"] = "另一个文件的前置样本"
        newsdata._write_path(newsdata.NEWSDATA_DIR / "a-prefix.csv", prefix)
        response, context = self.view("目标文件")
        match = context["rows"][0]
        self.assertEqual(match["_file"], row["_file"])
        self.assertEqual(match["_row"], 0)
        self.assertEqual(match["dataset_page"], 2)
        self.assertEqual(match["dataset_anchor"], "dataset-row-20")
        self.assertIn("/data?page=2#dataset-row-20", response.get_data(as_text=True))
        dataset_response = self.client.get("/data?page=2")
        self.assertEqual(dataset_response.status_code, 200)
        dataset_html = dataset_response.get_data(as_text=True)
        self.assertIn('id="dataset-row-20"', dataset_html)
        self.assertIn("目标文件消息", dataset_html)
        self.get_model.assert_not_called()

    def test_query_content_and_source_are_escaped_and_search_is_read_only(self):
        attack = '<script>alert("SEARCH_XSS")</script>'
        self.seed(attack, source='<img src=x onerror="SEARCH_XSS">')
        before = self.storage_snapshot()
        response, context = self.view(attack)
        self.assertEqual(context["rows"][0]["content"], attack)
        html = response.get_data(as_text=True)
        self.assertIn(str(escape(attack)), html)
        self.assertIn(str(escape('<img src=x onerror="SEARCH_XSS">')), html)
        self.assertNotIn(attack, html)
        self.assertNotIn('<img src=x onerror="SEARCH_XSS">', html)
        self.assertEqual(self.storage_snapshot(), before)

    def test_latest_report_links_do_not_replace_truth_labels_and_stale_links_disappear(self):
        row = self.seed("报告关联消息", nature="真实", fake_probability="91")
        reference = {"file": row["_file"], "row": int(row["_row"]), "signature": row["_signature"]}
        first = reports.save_report(row["content"], [], {"label": "低风险", "level": "low"})
        batches._link_report(reference, first, self.database_dir)
        latest = reports.save_report(row["content"], [], {"label": "高风险", "level": "high"})
        batches._link_report(reference, latest, self.database_dir)
        before = self.storage_snapshot()
        response, context = self.view("关联")
        match = context["rows"][0]
        self.assertEqual(match["nature"], "真实")
        self.assertEqual(match["fake_probability"], "91")
        self.assertEqual(match["latest_report"]["id"], latest["id"])
        self.assertEqual(match["latest_report"]["label"], "高风险")
        self.assertIn("/history/" + latest["id"], response.get_data(as_text=True))
        self.assertEqual(self.storage_snapshot(), before)
        newsdata.update_message(row["_file"], row["_row"], row["_signature"], {"content": "修改后的关联消息"})
        _, context = self.view("关联")
        self.assertIsNone(context["rows"][0]["latest_report"])
        self.assertEqual(reports.get_report(latest["id"])["message"], "报告关联消息")

    def test_storage_failure_returns_actionable_error_without_exception_details(self):
        with patch.object(newsdata, "load_all", side_effect=RuntimeError("PRIVATE_DATA_PATH")):
            response, context = self.view("关键字", status=503)
        self.assertIn("稍后重试", context["error"])
        self.assertNotIn("PRIVATE_DATA_PATH", response.get_data(as_text=True))
        self.assertEqual(context["rows"], [])

    def test_legacy_dataset_keyword_urls_redirect_to_the_independent_search_module(self):
        query = "NASA 新闻&%"
        with patch.object(newsdata, "load_all") as load:
            response = self.client.get("/data", query_string={"q": "  " + query + "  ", "page": 2})
        self.assertEqual(response.status_code, 302)
        target = urlsplit(response.headers["Location"])
        self.assertEqual(target.path, "/search")
        self.assertEqual(parse_qs(target.query), {"q": [query]})
        load.assert_not_called()
        self.seed(query)
        response = self.client.get(response.headers["Location"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.contexts[-1][0], "search.html")
        self.assertEqual(self.contexts[-1][1]["total_matches"], 1)
        self.get_model.assert_not_called()

    def test_dataset_page_links_to_search_without_an_embedded_search_input(self):
        self.seed("数据展示样本")
        response = self.client.get("/data")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.contexts[-1][0], "data.html")
        html = response.get_data(as_text=True)
        self.assertIn('href="/search"', html)
        self.assertNotIn('name="q"', html)
        self.assertNotIn("search_results", self.contexts[-1][1])
        self.get_model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
