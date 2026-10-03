"""Per-visitor API credentials: all credentials are fake and HTTP is mocked."""

import io
import json
import logging
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from checkmodel import deepseek_api
from webapp import api_credentials, batches, newsdata, reports

import test_detect_routes as route_helpers
import test_history_stream as stream_helpers


OWNER_KEY = "sk-test-only-owner-private-credential-09a7"
VISITOR_A = "sk-test-only-visitor-a-private-credential-b65d"
VISITOR_B = "sk-test-only-visitor-b-private-credential-f4e1"
VISITOR_NEW = "sk-test-only-replacement-private-credential-ec81"
SECRETS = (OWNER_KEY, VISITOR_A, VISITOR_B, VISITOR_NEW)
SAME_ORIGIN = {"Origin": "http://localhost"}


class VisitorCredentialTests(unittest.TestCase):
    capture_template = route_helpers.DetectRouteTests.capture_template
    lookup_sources = route_helpers.DetectRouteTests.lookup_sources
    lookup_model = route_helpers.DetectRouteTests.lookup_model
    database_snapshot = route_helpers.DetectRouteTests.database_snapshot
    events = stream_helpers.HistoryStreamTests.events

    def setUp(self):
        route_helpers.DetectRouteTests.setUp(self)
        self.stack.enter_context(patch.dict(os.environ, {
            "DEEPSEEK_API_KEY": OWNER_KEY,
            "DEEPSEEK_MODEL": "deepseek-flash",
            "DEEPSEEK_BASE_URL": "https://owner-proxy.invalid/v1",
        }, clear=True))
        self.stack.enter_context(patch.object(api_credentials, "_vault", {}))
        # Test the expiry callback without leaving eight-hour background timers.
        self.expiry_timer = self.stack.enter_context(patch.object(api_credentials.threading, "Timer"))
        self.stack.enter_context(patch.object(batches, "_job_cloud_models", {}))
        self.start_worker = self.stack.enter_context(patch.object(batches, "_start_worker"))
        self.owner_model = deepseek_api.DeepSeekAPI()
        self.owner_model.source = "cloud"
        self.owner_model.source_label = "云端 API"
        self.deepseek_sources[0].update(available=True, display_name=self.owner_model.display_name)
        self.deepseek_instances["cloud"] = self.owner_model
        self.client_a = self.client
        self.client_b = self.app.test_client()
        self.http_requests = []
        self.fail_keys = set()
        self.opener = MagicMock()
        self.opener.open.side_effect = self.mock_http
        self.stack.enter_context(patch.object(deepseek_api, "build_opener", return_value=self.opener))
        self.log_stream = io.StringIO()
        log_handler = logging.StreamHandler(self.log_stream)
        logging.getLogger().addHandler(log_handler)
        self.addCleanup(logging.getLogger().removeHandler, log_handler)

    def mock_http(self, request, timeout=None):
        self.http_requests.append(request)
        key = request.get_header("Authorization").removeprefix("Bearer ")
        self.assertNotEqual(key, OWNER_KEY, "A browser request must never use the website owner's credential")
        if key in self.fail_keys:
            raise HTTPError(request.full_url, 401, "PRIVATE_UPSTREAM " + key, {}, io.BytesIO(key.encode()))
        scores = {VISITOR_A: 21, VISITOR_B: 81, VISITOR_NEW: 51}
        self.assertIn(key, scores, "Unexpected credential reached mocked HTTP")
        content = json.dumps({"risk_score": scores[key], "reason": "访客风险分析说明"}, ensure_ascii=False)
        payload = json.dumps({"choices": [{"message": {"content": content}}]}, ensure_ascii=False).encode("utf-8")
        response = MagicMock()
        response.__enter__.return_value.read.return_value = payload
        return response

    def assert_no_secrets(self, value):
        raw = value if isinstance(value, bytes) else str(value).encode("utf-8")
        for key in SECRETS:
            self.assertNotIn(key.encode(), raw)

    def assert_safe_response(self, response):
        self.assert_no_secrets(response.data)
        self.assert_no_secrets(str(response.headers))

    def save_key(self, client, key, model="deepseek-flash"):
        response = client.post("/settings/api", json={"api_key": key, "model_name": model}, headers=SAME_ORIGIN)
        self.assertIn(response.status_code, (200, 201), response.get_data(as_text=True))
        self.assert_safe_response(response)
        self.assertEqual(response.get_json()["credentials"]["mode"], "personal")
        return response

    def state(self, client):
        response = client.get("/settings/api", headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 200)
        self.assert_safe_response(response)
        return response.get_json()["credentials"]

    def detect(self, client, endpoint="/detect/check", source="cloud"):
        response = client.post(endpoint, data={
            "message": "待分析的访客消息", "mode": "single",
            "models": "deepseek_r1", "deepseek_source": source,
        })
        self.assertEqual(response.status_code, 200)
        self.assert_safe_response(response)
        return response, self.contexts[-1][1]

    def create_batch(self, client):
        if newsdata.load_all().empty:
            imported = client.post("/data/import", data={"text": "访客批量消息"}, headers={"Accept": "application/json"})
            self.assertEqual(imported.status_code, 201)
        row = newsdata.load_all().iloc[0]
        response = client.post("/data/batches", json={
            "rows": [{"file": row["_file"], "row": int(row["_row"]), "signature": row["_signature"]}],
            "model_ids": ["deepseek_r1"], "mode": "single", "deepseek_source": "cloud",
        })
        self.assertEqual(response.status_code, 202)
        self.assert_safe_response(response)
        return response.get_json()

    def assert_public_storage_safe(self):
        self.assertNotIn("Bearer " + OWNER_KEY, [request.get_header("Authorization") for request in self.http_requests])
        for path in self.database_dir.rglob("*"):
            if path.is_file():
                self.assert_no_secrets(path.read_bytes())
        self.assert_no_secrets(json.dumps(reports.list_reports(), ensure_ascii=False))
        self.assert_no_secrets(json.dumps(batches.list_batches(), ensure_ascii=False))
        self.assert_no_secrets(self.log_stream.getvalue())

    def test_save_uses_opaque_http_only_cookies_and_never_prefills_password(self):
        for client, key in ((self.client_a, VISITOR_A), (self.client_b, VISITOR_B)):
            saved = self.save_key(client, key)
            cookie = client.get_cookie(api_credentials.COOKIE_NAME)
            self.assertIsNotNone(cookie)
            self.assertGreaterEqual(len(cookie.value), 32)
            self.assertTrue(cookie.http_only)
            self.assertEqual(cookie.same_site.lower(), "strict")
            self.assert_no_secrets(cookie.value)
            self.assertNotIn("Max-Age=", saved.headers.get("Set-Cookie", ""))
            page = client.get("/settings/api")
            self.assertEqual(page.status_code, 200)
            self.assert_safe_response(page)
            self.assertIn('type="password"', page.get_data(as_text=True))
            with client.session_transaction() as session:
                self.assert_no_secrets(json.dumps(dict(session)))
                self.assertNotIn("api_key", session)
        self.assertNotEqual(self.client_a.get_cookie(api_credentials.COOKIE_NAME).value,
                            self.client_b.get_cookie(api_credentials.COOKIE_NAME).value)
        self.opener.open.assert_not_called()
        self.assertEqual(os.environ["DEEPSEEK_API_KEY"], OWNER_KEY)
        self.assert_public_storage_safe()

    def test_json_and_strict_same_origin_checks_protect_save_and_clear(self):
        attacks = [
            ({}, True), ({"Origin": "https://foreign.invalid"}, True),
            ({"Origin": "null"}, True),
            ({"Origin": "https://foreign.invalid", "Referer": "http://localhost/settings/api"}, True),
            (SAME_ORIGIN, False),
        ]
        for headers, is_json in attacks:
            with self.subTest(headers=headers, json=is_json):
                arguments = {"json" if is_json else "data": {"api_key": VISITOR_A, "model_name": "deepseek-flash"}}
                response = self.client_a.post("/settings/api", headers=headers, **arguments)
                self.assertEqual(response.status_code, 403 if is_json else 400)
                self.assert_safe_response(response)
                self.assertIsNone(self.client_a.get_cookie(api_credentials.COOKIE_NAME))
        accepted = self.client_a.post("/settings/api", json={"api_key": VISITOR_A, "model_name": "deepseek-flash"},
                                      headers={"Referer": "http://localhost/settings/api"})
        self.assertEqual(accepted.status_code, 200)
        for headers in ({}, {"Origin": "https://foreign.invalid"}):
            response = self.client_a.post("/settings/api/clear", json={}, headers=headers)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(self.state(self.client_a)["mode"], "personal")
        self.opener.open.assert_not_called()

    def test_invalid_settings_leave_existing_credential_intact_and_errors_are_safe(self):
        self.save_key(self.client_a, VISITOR_A)
        token = self.client_a.get_cookie(api_credentials.COOKIE_NAME).value
        for payload in (
            {"api_key": ""}, {"api_key": VISITOR_B + "\nPRIVATE_UPSTREAM"},
            {"api_key": VISITOR_B, "model_name": VISITOR_NEW},
            {"api_key": [VISITOR_B]}, [VISITOR_B],
        ):
            response = self.client_a.post("/settings/api", json=payload, headers=SAME_ORIGIN)
            self.assertEqual(response.status_code, 400)
            self.assert_safe_response(response)
            self.assertEqual(self.client_a.get_cookie(api_credentials.COOKIE_NAME).value, token)
        self.assertEqual(self.state(self.client_a)["mode"], "personal")
        self.opener.open.assert_not_called()

    def test_https_settings_cookie_is_secure_and_json_does_not_expose_token(self):
        response = self.client_a.post("/settings/api", base_url="https://localhost",
                                      json={"api_key": VISITOR_A}, headers={"Origin": "https://localhost"})
        self.assertEqual(response.status_code, 200)
        cookie = self.client_a.get_cookie(api_credentials.COOKIE_NAME)
        self.assertTrue(cookie.secure)
        self.assertTrue(cookie.http_only)
        self.assertNotIn(cookie.value, response.get_data(as_text=True))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assert_safe_response(response)
        self.opener.open.assert_not_called()

    def test_personal_key_enables_cloud_without_site_default_and_uses_one_slot(self):
        self.deepseek_sources[0]["available"] = False
        self.assertEqual(self.state(self.client_a)["mode"], "missing")
        self.save_key(self.client_a, VISITOR_A, model="deepseek-v4-pro")
        response = self.client_a.get("/detect")
        self.assertEqual(response.status_code, 200)
        models = self.contexts[-1][1]["models"]
        deepseek = [model for model in models if model["id"] == "deepseek_r1"]
        self.assertEqual(len(deepseek), 1)
        self.assertTrue(deepseek[0]["available"])
        self.assertEqual(deepseek[0]["source"], "cloud")
        response = self.client_a.post("/detect/check", data={
            "message": "个人配置可以启用云端检测", "mode": "single", "models": "deepseek_r1",
        })
        self.assertEqual(response.status_code, 200)
        result = self.contexts[-1][1]["result"]
        self.assertEqual(result["selected_count"], 1)
        self.assertEqual(result["members"][0]["score"], 21)
        self.assertEqual(result["members"][0]["credential_mode"], "personal")
        self.assertEqual(json.loads(self.http_requests[0].data)["model"], "deepseek-v4-pro")
        self.assert_public_storage_safe()

    def test_two_clients_use_their_own_keys_in_sync_and_stream_without_owner_mutation(self):
        self.save_key(self.client_a, VISITOR_A)
        self.save_key(self.client_b, VISITOR_B)
        _, context = self.detect(self.client_a, "/detect")
        self.assertEqual(context["result"]["members"][0]["score"], 21)
        response = self.client_b.post("/detect/stream", data={
            "message": "第二位访客消息", "mode": "single", "models": "deepseek_r1", "deepseek_source": "cloud",
        }, buffered=False)
        try:
            events = list(self.events(response))
        finally:
            response.close()
        self.assertEqual(events[-1]["type"], "complete")
        self.assert_no_secrets(json.dumps(events, ensure_ascii=False))
        report = reports.get_report(events[-1]["record_id"])
        self.assertEqual(report["result"]["members"][0]["score"], 81)
        self.assertEqual([request.get_header("Authorization") for request in self.http_requests],
                         ["Bearer " + VISITOR_A, "Bearer " + VISITOR_B])
        self.assertTrue(all(request.full_url.startswith("https://api.deepseek.com/") for request in self.http_requests))
        self.assertEqual(os.environ["DEEPSEEK_API_KEY"], OWNER_KEY)
        self.assertEqual(self.owner_model._api_key, OWNER_KEY)
        self.assert_public_storage_safe()

    def test_visitor_key_does_not_override_explicit_local_source(self):
        self.save_key(self.client_a, VISITOR_A)
        _, context = self.detect(self.client_a, source="local")
        self.assertEqual(context["result"]["members"][0]["source"], "local")
        self.deepseek_instances["local"].check.assert_called_once()
        self.opener.open.assert_not_called()

    def test_401_for_visitor_key_is_safe_and_never_retried_with_owner_key(self):
        self.save_key(self.client_a, VISITOR_A)
        self.fail_keys.add(VISITOR_A)
        _, context = self.detect(self.client_a)
        member = context["result"]["members"][0]
        self.assertEqual(member["status"], "error")
        self.assertIsNone(member["score"])
        self.assertEqual(len(self.http_requests), 1)
        self.assertEqual(self.http_requests[0].get_header("Authorization"), "Bearer " + VISITOR_A)
        self.assertNotIn("PRIVATE_UPSTREAM", json.dumps(context["result"], ensure_ascii=False))
        self.assert_public_storage_safe()

    def test_expired_and_unknown_tokens_cannot_fall_back_to_owner(self):
        self.save_key(self.client_a, VISITOR_A)
        token = self.client_a.get_cookie(api_credentials.COOKIE_NAME).value
        api_credentials._vault[token]["expires_at"] = 0
        self.client_b.set_cookie(api_credentials.COOKIE_NAME, "unknown-visitor-token-never-in-vault")
        for client in (self.client_a, self.client_b):
            with self.subTest(client=client):
                self.assertEqual(self.state(client)["mode"], "expired")
                _, context = self.detect(client)
                member = context["result"]["members"][0]
                self.assertIn(member["status"], ("error", "unavailable"))
                self.assertIsNone(member["score"])
                self.assertIsNotNone(client.get_cookie(api_credentials.COOKIE_NAME))
                page = client.get("/detect")
                self.assertEqual(page.status_code, 200)
                deepseek = next(model for model in self.contexts[-1][1]["models"] if model["id"] == "deepseek_r1")
                self.assertEqual(deepseek["source"], "cloud")
                self.assertEqual(deepseek["default_source"], "cloud")
                self.assertFalse(deepseek["available"])
                response = client.post("/detect/check", data={
                    "message": "失效凭据不得静默更换模型来源", "mode": "single", "models": "deepseek_r1",
                })
                self.assertEqual(response.status_code, 200)
                member = self.contexts[-1][1]["result"]["members"][0]
                self.assertEqual(member["source"], "cloud")
                self.assertEqual(member["credential_mode"], "expired")
                self.assertIsNone(member["score"])
                self.deepseek_instances["local"].check.assert_not_called()
        self.opener.open.assert_not_called()
        self.assert_public_storage_safe()

    def test_scheduled_expiry_removes_idle_credentials_without_another_request(self):
        self.save_key(self.client_a, VISITOR_A)
        token_a = self.client_a.get_cookie(api_credentials.COOKIE_NAME).value
        scheduled_a = self.expiry_timer.call_args
        self.save_key(self.client_b, VISITOR_B)
        token_b = self.client_b.get_cookie(api_credentials.COOKIE_NAME).value
        self.assertEqual(scheduled_a.args[0], 8 * 60 * 60)
        self.assertEqual(scheduled_a.kwargs["args"], (token_a,))
        self.assertTrue(self.expiry_timer.return_value.daemon)
        self.assertEqual(self.expiry_timer.return_value.start.call_count, 2)
        scheduled_a.args[1](*scheduled_a.kwargs["args"])
        self.assertNotIn(token_a, api_credentials._vault)
        self.assertIn(token_b, api_credentials._vault)
        self.assertEqual(self.state(self.client_a)["mode"], "expired")
        self.assertEqual(self.state(self.client_b)["mode"], "personal")
        self.opener.open.assert_not_called()

    def test_explicit_clear_disables_cloud_without_affecting_other_visitor(self):
        self.save_key(self.client_a, VISITOR_A)
        self.save_key(self.client_b, VISITOR_B)
        cleared = self.client_a.post("/settings/api/clear", json={}, headers=SAME_ORIGIN)
        self.assertEqual(cleared.status_code, 200)
        self.assert_safe_response(cleared)
        self.assertIsNone(self.client_a.get_cookie(api_credentials.COOKIE_NAME))
        cleared_state = self.state(self.client_a)
        self.assertEqual(cleared_state["mode"], "missing")
        self.assertFalse(cleared_state["configured"])
        self.assertFalse(cleared_state["server_available"])
        self.assertEqual(self.state(self.client_b)["mode"], "personal")
        _, context = self.detect(self.client_a)
        self.assertEqual(context["result"]["success_count"], 0)
        self.assertIsNone(context["result"]["members"][0]["score"])
        self.opener.open.assert_not_called()

    def test_missing_personal_key_blocks_owner_cloud_in_sync_stream_and_metadata(self):
        self.assertTrue(self.owner_model.detect())
        state = self.state(self.client_a)
        self.assertEqual(state["mode"], "missing")
        self.assertFalse(state["configured"])
        self.assertFalse(state["server_available"])
        for path in ("/detect", "/data"):
            response = self.client_a.get(path)
            self.assertEqual(response.status_code, 200)
            deepseek = next(model for model in self.contexts[-1][1]["models"] if model["id"] == "deepseek_r1")
            cloud = next(source for source in deepseek["sources"] if source["source"] == "cloud")
            self.assertFalse(cloud["available"])
            self.assert_safe_response(response)
        for endpoint in ("/detect", "/detect/check"):
            _, context = self.detect(self.client_a, endpoint)
            member = context["result"]["members"][0]
            self.assertEqual(context["result"]["success_count"], 0)
            self.assertEqual(member["source"], "cloud")
            self.assertEqual(member["credential_mode"], "missing")
            self.assertIsNone(member["score"])
        response = self.client_b.post("/detect/stream", data={
            "message": "无个人凭据流式消息", "mode": "single",
            "models": "deepseek_r1", "deepseek_source": "cloud",
        }, buffered=False)
        try:
            events = list(self.events(response))
        finally:
            response.close()
        self.assertEqual(events[-1]["type"], "complete")
        report = reports.get_report(events[-1]["record_id"])
        self.assertEqual(report["result"]["success_count"], 0)
        self.assertEqual(report["result"]["members"][0]["credential_mode"], "missing")
        self.assert_no_secrets(json.dumps(events, ensure_ascii=False))
        self.opener.open.assert_not_called()
        self.assert_public_storage_safe()

    def test_missing_personal_key_keeps_local_available_but_never_falls_back_to_owner(self):
        _, context = self.detect(self.client_a, source="local")
        self.assertEqual(context["result"]["members"][0]["score"], 80)
        form = {"message": "未指定来源时只允许可用本地模型", "mode": "single", "models": "deepseek_r1"}
        response = self.client_a.post("/detect/check", data=form)
        self.assertEqual(response.status_code, 200)
        member = self.contexts[-1][1]["result"]["members"][0]
        self.assertEqual(member["source"], "local")
        self.assertEqual(member["score"], 80)
        self.deepseek_instances["local"].check.assert_called()
        self.deepseek_sources[1]["available"] = False
        response = self.client_a.post("/detect/check", data=form)
        self.assertEqual(response.status_code, 200)
        result = self.contexts[-1][1]["result"]
        self.assertEqual(result["success_count"], 0)
        self.assertIsNone(result["members"][0]["score"])
        self.opener.open.assert_not_called()
        self.assert_public_storage_safe()

    def test_missing_personal_key_rejects_cloud_only_batch_and_blocks_owner_in_mixed_batch(self):
        imported = self.client_a.post("/data/import", data={"text": "禁止复用站点密钥的批量消息"},
                                      headers={"Accept": "application/json"})
        self.assertEqual(imported.status_code, 201)
        row = newsdata.load_all().iloc[0]
        payload = {
            "rows": [{"file": row["_file"], "row": int(row["_row"]), "signature": row["_signature"]}],
            "model_ids": ["deepseek_r1"], "mode": "single", "deepseek_source": "cloud",
        }
        response = self.client_a.post("/data/batches", json=payload)
        self.assertEqual(response.status_code, 400)
        self.assert_safe_response(response)
        self.assertEqual(batches.list_batches(), [])
        self.start_worker.assert_not_called()
        payload.update(model_ids=["qwen2.5_7b", "deepseek_r1"], mode="vote")
        response = self.client_a.post("/data/batches", json=payload)
        self.assertEqual(response.status_code, 202)
        job_id = response.get_json()["job_id"]
        with self.app.app_context():
            batches.run_batch(job_id)
        result = batches.get_batch(job_id)["items"][0]["result"]
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["members"][1]["credential_mode"], "missing")
        self.assertIsNone(result["members"][1]["score"])
        self.opener.open.assert_not_called()
        self.assert_public_storage_safe()

    def test_batch_captures_visitor_key_at_creation_and_cleans_up_after_finish(self):
        self.save_key(self.client_a, VISITOR_A)
        self.save_key(self.client_b, VISITOR_B)
        job_a = self.create_batch(self.client_a)
        job_b = self.create_batch(self.client_b)
        self.assertIn(job_a["job_id"], batches._job_cloud_models)
        self.assertIn(job_b["job_id"], batches._job_cloud_models)
        self.save_key(self.client_a, VISITOR_NEW)
        self.client_b.post("/settings/api/clear", json={}, headers=SAME_ORIGIN)
        with self.app.app_context():
            batches.run_batch(job_a["job_id"])
            batches.run_batch(job_b["job_id"])
        self.assertEqual([request.get_header("Authorization") for request in self.http_requests],
                         ["Bearer " + VISITOR_A, "Bearer " + VISITOR_B])
        for created in (job_a, job_b):
            self.assertNotIn(created["job_id"], batches._job_cloud_models)
            response = self.client_a.get(created["status_url"])
            self.assert_safe_response(response)
            self.assertEqual(response.get_json()["job"]["saved_count"], 1)
        self.assert_public_storage_safe()

    def test_cancel_and_restart_clear_personal_job_snapshots_without_owner_calls(self):
        self.save_key(self.client_a, VISITOR_A)
        cancelled = self.create_batch(self.client_a)
        response = self.client_a.post(cancelled["cancel_url"], headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(cancelled["job_id"], batches._job_cloud_models)
        pending = self.create_batch(self.client_a)
        batches._job_cloud_models.clear()
        with patch.object(batches, "_initialized_paths", set()):
            batches.initialize()
        self.assertEqual(batches.get_batch(pending["job_id"])["status"], "interrupted")
        self.opener.open.assert_not_called()
        self.assert_public_storage_safe()

    def test_lost_personal_batch_snapshot_never_uses_default_credentials(self):
        self.save_key(self.client_a, VISITOR_A)
        created = self.create_batch(self.client_a)
        batches._job_cloud_models.pop(created["job_id"])
        with self.app.app_context():
            batches.run_batch(created["job_id"])
        job = batches.get_batch(created["job_id"])
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["failed_count"], 1)
        self.assertEqual(job["items"][0]["result"]["success_count"], 0)
        self.opener.open.assert_not_called()
        self.assert_public_storage_safe()

    def test_credentials_never_appear_on_public_pages_exports_or_session(self):
        self.save_key(self.client_a, VISITOR_A)
        self.detect(self.client_a)
        record_id = reports.list_reports()["records"][0]["id"]
        paths = ["/", "/settings/api", "/detect", "/data", "/history", "/history/" + record_id]
        paths += ["/history/{}/export?format={}".format(record_id, extension) for extension in ("json", "csv", "txt")]
        for path in paths:
            with self.subTest(path=path):
                response = self.client_a.get(path)
                self.assertEqual(response.status_code, 200)
                self.assert_safe_response(response)
        with self.client_a.session_transaction() as session:
            self.assert_no_secrets(json.dumps(dict(session)))
        self.assert_public_storage_safe()


if __name__ == "__main__":
    unittest.main()
