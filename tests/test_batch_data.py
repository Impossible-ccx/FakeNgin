"""Dataset import and persisted batch-job integration tests; no real inference."""

import io
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from markupsafe import escape
from werkzeug.datastructures import MultiDict
import checkmodel
from webapp import batches, create_app, newsdata, reports

import test_detect_routes as helpers


class BatchDataTests(unittest.TestCase):
    capture_template = helpers.DetectRouteTests.capture_template
    lookup_model = helpers.DetectRouteTests.lookup_model
    database_snapshot = helpers.DetectRouteTests.database_snapshot

    def setUp(self):
        helpers.DetectRouteTests.setUp(self)
        self.start_worker = self.stack.enter_context(patch.object(batches, "_start_worker"))
        self.workers = []
        self.worker_errors = []
        self.releases = []
        self.addCleanup(self.stop_workers)

    def stop_workers(self):
        for release in self.releases:
            release.set()
        for worker, _ in self.workers:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive(), "Test worker did not stop before database cleanup")
        self.assertFalse(self.worker_errors, repr(self.worker_errors))

    def import_text(self, text):
        return self.client.post("/data/import", data={"text": text}, headers={"Accept": "application/json"})

    def seed_rows(self, messages):
        response = self.import_text("\n".join(messages))
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["imported_count"], len(messages))
        return self.refs()

    def refs(self):
        return [
            {"file": row["_file"], "row": int(row["_row"]), "signature": row["_signature"]}
            for row in newsdata.load_all().to_dict("records")
        ]

    def create_job(self, refs, **overrides):
        payload = {"rows": refs, "model_ids": helpers.RISK_IDS, "mode": "vote", "deepseek_source": "local"}
        payload.update(overrides)
        response = self.client.post("/data/batches", json=payload)
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        return response.get_json()

    def job(self, job_id, client=None):
        response = (client or self.client).get("/data/batches/" + job_id)
        self.assertEqual(response.status_code, 200)
        return response.get_json()["job"]

    def run_job(self, job_id):
        with self.app.app_context():
            batches.run_batch(job_id)
        return self.job(job_id)

    def start_test_worker(self, job_id):
        done = threading.Event()

        def work():
            try:
                with self.app.app_context():
                    batches.run_batch(job_id)
            except BaseException as exc:
                self.worker_errors.append(exc)
            finally:
                done.set()

        thread = threading.Thread(target=work, daemon=True)
        self.workers.append((thread, done))
        thread.start()
        return thread, done

    def pause_on_second_row(self, first="第一条消息", second="第二条消息"):
        entered = threading.Event()
        release = threading.Event()
        self.releases.append(release)
        scores = {first: (10, 50, 90), second: (80, 82, 10)}
        for index, model_id in enumerate(helpers.RISK_IDS):
            def check(message, index=index):
                if message == second and index == 0:
                    entered.set()
                    if not release.wait(timeout=5):
                        raise RuntimeError("Test did not release blocked model")
                return scores.get(message, (80, 80, 80))[index], "批量风险说明"
            self.model_instances[model_id].check.side_effect = check
        return entered, release

    def test_empty_dataset_exposes_import_and_batch_controls_without_inference(self):
        response = self.client.get("/data")
        self.assertEqual(response.status_code, 200)
        context = self.contexts[-1][1]
        self.assertEqual(context["total"], 0)
        self.assertEqual(context["rows"], [])
        self.assertIn("batch_limits", context)
        self.assertIn("models", context)
        html = response.get_data(as_text=True)
        self.assertIn("/data/import", html)
        self.assertIn("/data/batches", html)
        self.assertIn('href="/verify"', html)
        self.get_model.assert_not_called()

    def test_fourth_registered_adapter_participates_in_a_three_member_batch_vote(self):
        fourth_id = helpers.register_fourth_adapter(self)
        refs = self.seed_rows(["使用新登记模型进行批量检测"])
        selected = helpers.RISK_IDS[:2] + [fourth_id]
        created = self.create_job(refs, model_ids=selected)
        job = self.run_job(created["job_id"])
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["saved_count"], 1)
        report = reports.get_report(job["items"][0]["record_id"])
        self.assertEqual(report["model_ids"], selected)
        self.assertEqual([member["id"] for member in report["result"]["members"]], selected)
        self.assertEqual(report["result"]["decision_method"], "majority")
        self.model_instances[helpers.RISK_IDS[2]].check.assert_not_called()

    def test_import_text_and_csv_preserve_chinese_and_never_start_detection(self):
        response = self.import_text("第一条消息\n\n第二条消息\n")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["imported_count"], 2)
        csv_bytes = '\ufeff正文,来源\n"有逗号,也有换行\n仍是同一条消息",课程数据\n第三条CSV消息,来源甲\n'.encode("utf-8")
        response = self.client.post(
            "/data/import", data={"file": (io.BytesIO(csv_bytes), "中文样本.csv")},
            headers={"Accept": "application/json"},
        )
        self.assertEqual(response.status_code, 201)
        payload = response.get_json()
        self.assertEqual(payload["imported_count"], 2)
        self.assertEqual(Path(payload["filename"]).name, payload["filename"])
        rows = newsdata.load_all()
        self.assertEqual(len(rows), 4)
        self.assertIn("有逗号,也有换行\n仍是同一条消息", rows.content.tolist())
        self.assertIn("课程数据", rows.source.tolist())
        self.get_model.assert_not_called()
        self.start_worker.assert_not_called()
        self.assertEqual(reports.list_reports()["total"], 0)

    def test_import_limits_reject_atomically_and_accept_500_rows(self):
        response = self.import_text("\n".join("合法消息 {}".format(index) for index in range(500)))
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["imported_count"], 500)
        before = self.database_snapshot()
        bad_forms = [
            {"text": "\n".join("超额消息 {}".format(index) for index in range(501))},
            {"text": "合法首行\n" + "字" * 6001},
            {"text": "   \n\t"},
            {"text": "消息", "source": "来" * 201},
            {"file": (io.BytesIO(("内容,来源\n消息," + "来" * 201 + "\n").encode("utf-8")), "long-source.csv")},
            {"file": (io.BytesIO(b"content\n" + b"x" * (1024 * 1024)), "large.csv")},
            {"file": (io.BytesIO(b"wrong_column\nmissing content\n"), "wrong.csv")},
            {"text": "消息", "file": (io.BytesIO(b"content\nother\n"), "both.csv")},
        ]
        for form in bad_forms:
            with self.subTest(fields=list(form)):
                response = self.client.post("/data/import", data=form, headers={"Accept": "application/json"})
                try:
                    self.assertIn(response.status_code, (400, 413))
                    self.assertEqual(self.database_snapshot(), before)
                finally:
                    # Werkzeug may spool the oversized multipart request to a file.
                    response.request.input_stream.close()
                    response.close()
        self.get_model.assert_not_called()
        self.start_worker.assert_not_called()

    def test_duplicate_stale_and_invalid_selections_never_launch_worker(self):
        refs = self.seed_rows(["原始内容", "第二条"])
        table = newsdata.read_table(refs[0]["file"])
        table.at[0, "content"] = "修改后的内容"
        newsdata.write_table(refs[0]["file"], table)
        current = self.refs()
        bad_refs = [
            [current[1], current[1]], [current[1], refs[0]],
            [{**current[0], "file": "../users.csv"}],
            [{**current[0], "row": -1}], [{**current[0], "row": 999}],
            [{**current[0], "signature": "tampered"}], [],
        ]
        before = self.database_snapshot()
        for selection in bad_refs:
            with self.subTest(selection=selection):
                response = self.client.post("/data/batches", json={
                    "rows": selection, "model_ids": helpers.RISK_IDS, "mode": "vote", "deepseek_source": "local",
                })
                self.assertEqual(response.status_code, 400)
                self.start_worker.assert_not_called()
                self.get_model.assert_not_called()
                self.assertEqual(self.database_snapshot(), before)
        self.assertEqual(batches.list_batches(), [])

    def test_batch_size_limit_and_invalid_model_selection_precede_inference(self):
        refs = self.seed_rows(["消息 {}".format(index) for index in range(101)])
        payload = {"rows": refs, "model_ids": helpers.RISK_IDS, "mode": "vote", "deepseek_source": "local"}
        self.assertEqual(self.client.post("/data/batches", json=payload).status_code, 400)
        for changes in ({"model_ids": ["deepseek_r1", "deepseek_r1"]}, {"deepseek_source": "both"}, {"mode": "unknown"}):
            response = self.client.post("/data/batches", json={**payload, "rows": refs[:1], **changes})
            self.assertEqual(response.status_code, 400)
        self.start_worker.assert_not_called()
        self.get_model.assert_not_called()
        created = self.create_job(refs[:100])
        self.assertEqual(created["job"]["total"], 100)
        self.start_worker.assert_called_once()
        self.get_model.assert_not_called()

    def test_pending_job_can_be_reopened_without_duplicate_work(self):
        created = self.create_job(self.seed_rows(["待处理消息"]))
        self.assertEqual(created["job_url"], "/data?job=" + created["job_id"])
        self.assertEqual(self.job(created["job_id"])["status"], "queued")
        response = self.client.get(created["job_url"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.contexts[-1][1]["current_job"]["id"], created["job_id"])
        rebuilt = create_app()
        rebuilt.config.update(TESTING=True)
        self.assertEqual(self.job(created["job_id"], rebuilt.test_client())["status"], "queued")
        self.start_worker.assert_called_once()
        self.get_model.assert_not_called()

    def test_preflight_requires_an_available_selected_model_but_allows_partial_availability(self):
        refs = self.seed_rows(["可用性验证消息"])
        self.available_models.return_value = []
        response = self.client.post("/data/batches", json={
            "rows": refs, "model_ids": helpers.RISK_IDS, "mode": "vote", "deepseek_source": "local",
        })
        self.assertEqual(response.status_code, 400)
        self.available_models.return_value = helpers.RISK_MODELS
        response = self.client.post("/data/batches", json={
            "rows": refs, "model_ids": ["deepseek_r1"], "mode": "single", "deepseek_source": "cloud",
        })
        self.assertEqual(response.status_code, 400)
        self.start_worker.assert_not_called()
        self.get_model.assert_not_called()
        self.assertEqual(batches.list_batches(), [])
        self.available_models.return_value = helpers.RISK_MODELS[:1]
        unavailable_model = self.model_instances.pop(helpers.RISK_IDS[1])
        created = self.create_job(refs, model_ids=helpers.RISK_IDS[:2], deepseek_source="local")
        job = self.run_job(created["job_id"])
        result = job["items"][0]["result"]
        self.assertEqual(result["selected_count"], 2)
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["decision_method"], "mean_fallback")
        self.assertEqual(result["members"][1]["status"], "unavailable")
        unavailable_model.check.assert_not_called()
        self.get_model.assert_any_call(helpers.RISK_IDS[0])
        self.get_model.assert_any_call(helpers.RISK_IDS[1])

    def test_no_js_import_and_batch_forms_redirect_to_resumable_job(self):
        response = self.client.post("/data/import", data={"text": "普通表单消息"})
        self.assertEqual(response.status_code, 303)
        self.assertIn("/data", response.headers["Location"])
        refs = self.refs()
        self.assertEqual(len(refs), 1)
        form = MultiDict([
            ("rows", json.dumps(refs[0])), ("models", helpers.RISK_IDS[0]),
            ("mode", "single"), ("deepseek_source", "local"),
        ])
        response = self.client.post("/data/batches", data=form)
        self.assertEqual(response.status_code, 303)
        self.assertIn("/data?job=", response.headers["Location"])
        resumed = self.client.get(response.headers["Location"])
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(self.contexts[-1][1]["current_job"]["total"], 1)
        self.start_worker.assert_called_once()
        self.get_model.assert_not_called()

    def test_forged_cloud_batch_is_rejected_without_calling_owner_model(self):
        refs = self.seed_rows(["云端批量样本"])
        cloud = Mock(score_kind="risk", check=Mock(return_value=(12, "站点私有模型")))
        with patch.object(checkmodel, "get_model", return_value=cloud) as cloud_lookup:
            response = self.client.post("/data/batches", json={
                "rows": refs, "model_ids": helpers.RISK_IDS, "mode": "vote", "deepseek_source": "cloud",
            })
            cloud_lookup.assert_not_called()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(batches.list_batches(), [])
        self.get_model.assert_not_called()
        self.start_worker.assert_not_called()
        cloud.check.assert_not_called()
        for model in self.model_instances.values():
            model.check.assert_not_called()

    def test_legacy_cloud_batch_is_interrupted_without_running_any_model(self):
        refs = self.seed_rows(["旧版云端任务"])
        created = self.create_job(refs)
        legacy = batches.get_batch(created["job_id"])
        legacy["deepseek_source"] = "cloud"
        batches._save_job(legacy)
        result = self.run_job(created["job_id"])
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["completed_count"], 0)
        self.assertEqual(reports.list_reports()["total"], 0)
        self.get_model.assert_not_called()
        for model in self.model_instances.values():
            model.check.assert_not_called()

    def test_each_row_is_saved_before_next_finishes_and_dataset_links_latest_report(self):
        refs = self.seed_rows(["第一条消息", "第二条消息"])
        csv_before = self.database_snapshot()
        entered, release = self.pause_on_second_row()
        created = self.create_job(refs)
        thread, done = self.start_test_worker(created["job_id"])
        self.assertTrue(entered.wait(timeout=5), "Second model call was not reached")
        progress = self.job(created["job_id"])
        self.assertEqual(progress["completed_count"], 1)
        self.assertEqual(progress["saved_count"], 1)
        self.assertTrue(any(
            member["id"] == helpers.RISK_IDS[0] and member["status"] == "running"
            for member in progress["items"][1]["members"]
        ), "The running member must be persisted before its model call returns")
        first_id = progress["items"][0]["record_id"]
        self.assertTrue(first_id)
        self.assertEqual(reports.list_reports()["total"], 1)
        first = reports.get_report(first_id)
        self.assertEqual(first["result"]["decision_method"], "mean_fallback")
        self.assertEqual(first["result"]["mean_score"], 50)
        self.assertEqual(first["result"]["batch_id"], created["job_id"])
        self.assertEqual(first["result"]["batch_index"], 0)
        self.assertEqual(first["result"]["batch_total"], 2)
        self.assertEqual(first["result"]["dataset_ref"], refs[0])
        self.assertTrue(first["prompt_version"])
        self.assertEqual(first["result"]["members"][1]["id"], helpers.RISK_IDS[1])
        response = self.client.get("/data")
        rows = self.contexts[-1][1]["rows"]
        self.assertEqual(rows[0]["latest_report"]["id"], first_id)
        self.assertIn("/history/" + first_id, response.get_data(as_text=True))
        self.assertIsNone(rows[1]["latest_report"])
        rebuilt = create_app()
        rebuilt.config.update(TESTING=True)
        self.assertEqual(self.job(created["job_id"], rebuilt.test_client())["status"], "running")
        release.set()
        self.assertTrue(done.wait(timeout=5))
        thread.join(timeout=5)
        finished = self.job(created["job_id"])
        self.assertEqual(finished["status"], "completed")
        self.assertEqual((finished["completed_count"], finished["saved_count"], finished["failed_count"]), (2, 2, 0))
        self.assertEqual(finished["items"][1]["result"]["decision_method"], "majority")
        self.assertEqual(reports.list_reports()["total"], 2)
        self.assertEqual(self.database_snapshot(), csv_before)

    def test_failed_row_is_saved_and_does_not_stop_following_row(self):
        refs = self.seed_rows(["第一条失败", "第二条成功"])
        for model in self.model_instances.values():
            def check(message):
                if message == "第一条失败":
                    raise RuntimeError("PRIVATE_UPSTREAM_DETAIL")
                return 80, "正常说明"
            model.check.side_effect = check
        created = self.create_job(refs)
        job = self.run_job(created["job_id"])
        self.assertEqual(job["status"], "completed")
        self.assertEqual((job["completed_count"], job["saved_count"], job["failed_count"]), (2, 2, 1))
        self.assertEqual(job["items"][0]["result"]["decision_method"], "unavailable")
        self.assertEqual(job["items"][1]["result"]["level"], "high")
        self.assertTrue(all(item["record_id"] for item in job["items"]))
        self.assertNotIn("PRIVATE_UPSTREAM_DETAIL", json.dumps(job, ensure_ascii=False))

    def test_batch_model_aliases_are_ui_only_and_raw_report_metadata_is_preserved(self):
        refs = self.seed_rows(["报告原始元数据样本"])
        created = self.create_job(refs, model_ids=[helpers.RISK_IDS[2]], mode="single")
        job = self.run_job(created["job_id"])
        item = job["items"][0]
        self.assertEqual(item["members"][0]["ui_display_name"], "分析模型 3")
        self.assertNotIn("ui_display_name", item["result"]["members"][0])
        stored = reports.get_report(item["record_id"])
        raw = stored["result"]["members"][0]
        self.assertEqual(raw["id"], helpers.RISK_IDS[2])
        self.assertEqual(raw["display_name"], helpers.RISK_MODELS[2]["display_name"])
        self.assertNotIn("ui_display_name", raw)
        self.assertEqual(stored["result"], item["result"])
        exported = self.client.get(item["history_url"] + "/export?format=json").get_json()
        self.assertEqual(exported["result"], item["result"])

    def test_search_links_completed_batch_report_only_while_original_content_matches(self):
        refs = self.seed_rows(["可检索原文", "其他不相关消息"])
        created = self.create_job(refs[:1])
        job = self.run_job(created["job_id"])
        report_id = job["items"][0]["record_id"]
        self.assertTrue(report_id)
        original_report = reports.get_report(report_id)
        call_counts = [model.check.call_count for model in self.model_instances.values()]
        self.get_model.reset_mock()

        response = self.client.get("/search", query_string={"q": "可检索原文"})
        self.assertEqual(response.status_code, 200)
        context = self.contexts[-1][1]
        self.assertEqual(context["total_matches"], 1)
        self.assertEqual(context["rows"][0]["latest_report"]["id"], report_id)
        self.assertIn("/history/" + report_id, response.get_data(as_text=True))

        newsdata.update_message(
            refs[0]["file"], refs[0]["row"], refs[0]["signature"],
            {"content": "编辑后的可检索原文"},
        )
        response = self.client.get("/search", query_string={"q": "可检索原文"})
        self.assertEqual(response.status_code, 200)
        context = self.contexts[-1][1]
        self.assertEqual(context["total_matches"], 1)
        self.assertIsNone(context["rows"][0]["latest_report"])
        self.assertNotIn("/history/" + report_id, response.get_data(as_text=True))
        self.assertEqual(self.client.get("/history/" + report_id).status_code, 200)
        self.assertEqual(reports.get_report(report_id), original_report)
        self.assertEqual(reports.list_reports()["total"], 1)
        self.get_model.assert_not_called()
        self.assertEqual(
            [model.check.call_count for model in self.model_instances.values()], call_counts,
        )

    def test_cancel_finishes_current_call_keeps_saved_rows_and_stops_remaining_models(self):
        refs = self.seed_rows(["第一条消息", "第二条消息", "第三条消息"])
        entered, release = self.pause_on_second_row()
        created = self.create_job(refs)
        thread, done = self.start_test_worker(created["job_id"])
        self.assertTrue(entered.wait(timeout=5))
        response = self.client.post(created["cancel_url"], headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["job"]["cancel_requested"])
        release.set()
        self.assertTrue(done.wait(timeout=5))
        thread.join(timeout=5)
        job = self.job(created["job_id"])
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["completed_count"], 1)
        self.assertEqual(job["saved_count"], 1)
        self.assertTrue(job["items"][0]["record_id"])
        self.assertFalse(job["items"][1]["record_id"])
        self.assertFalse(job["items"][2]["record_id"])
        self.assertEqual(self.model_instances[helpers.RISK_IDS[0]].check.call_count, 2)
        self.assertEqual(self.model_instances[helpers.RISK_IDS[1]].check.call_count, 1)
        self.assertEqual(self.model_instances[helpers.RISK_IDS[2]].check.call_count, 1)
        self.assertEqual(reports.list_reports()["total"], 1)

    def test_history_save_failure_preserves_result_and_continues_batch(self):
        refs = self.seed_rows(["保存失败消息", "保存成功消息"])
        real_save = reports.save_report
        calls = []

        def save(*args, **kwargs):
            calls.append(args[0])
            if len(calls) == 1:
                raise OSError("PRIVATE_DATABASE_DETAIL")
            return real_save(*args, **kwargs)

        created = self.create_job(refs)
        with patch.object(reports, "save_report", side_effect=save):
            job = self.run_job(created["job_id"])
        self.assertEqual(job["status"], "completed")
        self.assertEqual((job["completed_count"], job["saved_count"], job["save_failed_count"]), (2, 1, 1))
        self.assertEqual(job["items"][0]["result"]["level"], "high")
        self.assertFalse(job["items"][0]["record_id"])
        self.assertTrue(job["items"][0]["history_error"])
        self.assertTrue(job["items"][1]["record_id"])
        self.assertNotIn("PRIVATE_DATABASE_DETAIL", json.dumps(job, ensure_ascii=False))

    def test_process_recovery_marks_unfinished_job_interrupted_without_retry(self):
        refs = self.seed_rows(["已完成样本", "尚未执行样本"])
        completed = self.create_job(refs[:1])
        self.run_job(completed["job_id"])
        pending = self.create_job(refs[1:])
        self.get_model.reset_mock()
        self.start_worker.reset_mock()
        with patch.object(batches, "_initialized_paths", set()):
            batches.initialize()
        self.assertEqual(self.job(completed["job_id"])["status"], "completed")
        self.assertEqual(self.job(pending["job_id"])["status"], "interrupted")
        self.assertEqual(reports.list_reports()["total"], 1)
        self.get_model.assert_not_called()
        self.start_worker.assert_not_called()

    def test_imported_message_and_batch_reason_are_escaped_in_rendered_pages(self):
        message = '<script>alert("dataset")</script>'
        reason = '<img src=x onerror=alert("batch")>'
        refs = self.seed_rows([message])
        for model in self.model_instances.values():
            model.check.return_value = (80, reason)
        created = self.create_job(refs)
        job = self.run_job(created["job_id"])
        response = self.client.get(created["job_url"])
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertNotIn(message, html)
        self.assertNotIn(reason, html)
        self.assertIn(str(escape(message)), html)
        detail = self.client.get(job["items"][0]["history_url"]).get_data(as_text=True)
        self.assertIn(str(escape(reason)), detail)
        self.assertNotIn(reason, detail)

    def test_unknown_batch_poll_and_cancel_return_404(self):
        self.assertEqual(self.client.get("/data/batches/not-a-job").status_code, 404)
        self.assertEqual(self.client.post("/data/batches/not-a-job/cancel").status_code, 404)
        self.get_model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
