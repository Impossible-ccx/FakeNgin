"""Manual verification workflows against temporary CSV/SQLite storage only."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from markupsafe import escape
from webapp import db, newsdata, reports

import test_detect_routes as helpers


class VerifyRouteTests(unittest.TestCase):
    capture_template = helpers.DetectRouteTests.capture_template
    lookup_sources = helpers.DetectRouteTests.lookup_sources
    lookup_model = helpers.DetectRouteTests.lookup_model
    database_snapshot = helpers.DetectRouteTests.database_snapshot

    def setUp(self):
        helpers.DetectRouteTests.setUp(self)

    def login(self, client=None):
        response = (client or self.client).post("/login", data={
            "username": db.DEFAULT_USER["username"], "password": db.DEFAULT_USER["password"],
        })
        self.assertEqual(response.status_code, 302)

    def seed(self, content="待人工校验的消息", **values):
        newsdata.append_message({"content": content, "nature": newsdata.DEFAULT_NATURE, **values})
        return newsdata.load_all().iloc[-1].to_dict()

    def ref(self, row):
        return {key: row[key] for key in ("_file", "_row", "_signature")}

    def rows(self):
        return newsdata.load_all().to_dict("records")

    def view(self, path="/verify"):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.contexts[-1][0], "verify.html")
        return response, self.contexts[-1][1]

    def mutate(self, action, payload=None):
        response = self.client.post("/verify/" + action, data=payload or {})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/verify", response.headers["Location"])
        return response

    def test_all_manual_endpoints_require_login_and_leave_database_untouched(self):
        row = self.seed()
        before = self.database_snapshot()
        response = self.client.get("/verify")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])
        for action in ("add", "update", "delete", "check", "skip", "reset_skip"):
            with self.subTest(action=action):
                response = self.client.post("/verify/" + action, data={
                    **self.ref(row), "content": "未经登录的修改", "nature": "虚假",
                })
                self.assertEqual(response.status_code, 302)
                self.assertIn("/login", response.headers["Location"])
                self.assertEqual(self.database_snapshot(), before)
        self.get_model.assert_not_called()

    def test_empty_state_add_edit_delete_and_label_survive_reload(self):
        self.login()
        _, empty = self.view()
        self.assertEqual(empty["total"], 0)
        self.assertIsNone(empty["check_row"])
        self.mutate("add", {"content": "人工新建消息", "source": "课程样本", "nature": "未校验"})
        row = self.rows()[0]
        self.assertEqual(row["_file"], newsdata.MANUAL_FILE)
        self.assertEqual(row["content"], "人工新建消息")
        self.mutate("update", {**self.ref(row), "content": "人工修改消息", "source": "修改后来源", "nature": "未校验"})
        row = self.rows()[0]
        self.assertEqual(row["content"], "人工修改消息")
        self.mutate("check", {**self.ref(row), "nature": "真实"})
        row = self.rows()[0]
        self.assertEqual(row["nature"], "真实")
        self.assertTrue(row["process_time"])
        _, context = self.view()
        self.assertEqual(context["check_state"], "all_verified")
        self.assertEqual(context["rows"][0]["nature"], "真实")
        self.mutate("delete", self.ref(row))
        self.assertEqual(self.rows(), [])
        self.get_model.assert_not_called()

    def test_check_accepts_only_manual_truth_labels_without_changing_model_risk(self):
        self.login()
        expected_risk = {"risk_score": "88", "risk_model": "model-x", "risk_reason": "原风险说明", "risk_prompt_version": "risk-v1"}
        self.seed(**expected_risk)
        for nature in ("虚假", "真实", "中立"):
            row = self.rows()[0]
            self.mutate("check", {**self.ref(row), "nature": nature, "risk_score": "0"})
            updated = self.rows()[0]
            self.assertEqual(updated["nature"], nature)
            self.assertEqual({key: updated[key] for key in expected_risk}, expected_risk)
        self.assertEqual(set(newsdata.VERIFY_NATURES), {"虚假", "真实", "中立"})
        before = self.database_snapshot()
        for nature in ("未校验", "高风险", "", "<script>label</script>"):
            self.mutate("check", {**self.ref(self.rows()[0]), "nature": nature})
            self.assertEqual(self.database_snapshot(), before)

    def test_skip_is_per_browser_identifies_duplicate_rows_and_reset_restores_queue(self):
        self.login()
        self.seed("同样正文")
        self.seed("同样正文")
        before = self.database_snapshot()
        _, context = self.view()
        first = context["check_row"]
        self.mutate("skip", self.ref(first))
        _, context = self.view()
        second = context["check_row"]
        self.assertNotEqual((first["_file"], first["_row"]), (second["_file"], second["_row"]))
        self.assertEqual(context["check_state"], "ready")
        self.mutate("skip", self.ref(second))
        _, context = self.view()
        self.assertEqual(context["check_state"], "all_skipped")
        self.assertIsNone(context["check_row"])
        self.assertEqual(self.database_snapshot(), before)
        other = self.app.test_client()
        self.login(other)
        response = other.get("/verify")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.contexts[-1][1]["check_state"], "ready")
        self.mutate("reset_skip")
        _, context = self.view()
        self.assertEqual(context["check_state"], "ready")
        self.assertEqual(context["check_row"]["_row"], first["_row"])

    def test_missing_stale_or_invalid_references_cannot_modify_delete_or_skip_rows(self):
        self.login()
        stale = self.seed("原始正文")
        self.seed("另一条正文")
        newsdata.update_message(stale["_file"], int(stale["_row"]), stale["_signature"], {"source": "并发修改来源"})
        current = self.rows()[0]
        invalid_refs = [
            self.ref(stale), {**self.ref(current), "_signature": ""},
            {**self.ref(current), "_signature": "tampered"},
            {**self.ref(current), "_row": -1}, {**self.ref(current), "_row": 999},
            {**self.ref(current), "_row": "not-a-row"},
            {**self.ref(current), "_file": "../users.csv"},
            {**self.ref(current), "_file": str(db.USERS_FILE)},
        ]
        before = self.database_snapshot()
        for action in ("update", "delete", "check", "skip"):
            for reference in invalid_refs:
                with self.subTest(action=action, reference=reference):
                    self.mutate(action, {**reference, "content": "错误修改", "nature": "虚假"})
                    self.assertEqual(self.database_snapshot(), before)
        _, context = self.view()
        self.assertEqual(context["check_state"], "ready")
        self.assertEqual(context["check_row"]["_row"], current["_row"])

    def test_validation_is_atomic_and_user_content_is_html_escaped(self):
        self.login()
        message = '<script>alert("manual")</script>'
        source = '<img src=x onerror="alert(1)">'
        self.mutate("add", {"content": message, "source": source, "nature": "未校验", "risk_score": "99"})
        row = self.rows()[0]
        self.assertEqual(row["risk_score"], "")
        response, _ = self.view()
        html = response.get_data(as_text=True)
        self.assertNotIn(message, html)
        self.assertNotIn(source, html)
        self.assertIn(str(escape(message)), html)
        self.assertIn(str(escape(source)), html)
        before = self.database_snapshot()
        for bad in ({"content": " "}, {"content": "字" * 6001}, {"source": "来" * 201},
                    {"nature": "不支持"}, {"publish_time": "bad-date"}, {"fake_probability": "NaN"},
                    {"fake_probability": "101"}):
            for action in ("add", "update"):
                with self.subTest(action=action, bad=list(bad)):
                    self.mutate(action, {**self.ref(row), "content": "合法正文", "nature": "未校验", **bad})
                    self.assertEqual(self.database_snapshot(), before)

    def test_editing_content_clears_old_risk_fields_but_preserves_saved_report(self):
        self.login()
        row = self.seed("报告中的原文", risk_score="88", risk_model="old-model", risk_reason="旧风险理由", risk_prompt_version="risk-v1")
        response = self.client.post("/detect/check", data={
            "message": row["content"], "models": "qwen2.5_7b", "mode": "single", "deepseek_source": "local",
        })
        self.assertEqual(response.status_code, 200)
        record_id = reports.list_reports()["records"][0]["id"]
        report_before = reports.get_report(record_id)
        self.mutate("update", {**self.ref(row), "content": row["content"], "source": "仅修改来源", "nature": "未校验"})
        row = self.rows()[0]
        self.assertEqual(row["risk_score"], "88")
        self.mutate("update", {**self.ref(row), "content": "正文已经修改", "nature": "未校验", "risk_score": "100"})
        updated = self.rows()[0]
        self.assertTrue(all(updated[column] == "" for column in newsdata.RISK_COLUMNS))
        self.assertNotEqual(updated["_signature"], row["_signature"])
        self.assertEqual(reports.get_report(record_id), report_before)
        self.assertEqual(report_before["message"], "报告中的原文")

    def test_pagination_and_manual_navigation_keep_access_to_later_rows(self):
        self.login()
        for index in range(23):
            self.seed("分页消息 {}".format(index))
        response, first = self.view()
        self.assertEqual(first["total"], 23)
        self.assertEqual(first["page"], 1)
        self.assertEqual(len(first["rows"]), 20)
        response, second = self.view("/verify?page=2")
        self.assertEqual(second["page"], 2)
        self.assertEqual(len(second["rows"]), 3)
        row = second["rows"][0]
        response = self.mutate("update", {**self.ref(row), "content": row["content"], "nature": "中立", "page": "2"})
        self.assertIn("page=2", response.headers["Location"])
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('href="/verify"', html)
        self.assertIn("人工校验", html)


if __name__ == "__main__":
    unittest.main()
