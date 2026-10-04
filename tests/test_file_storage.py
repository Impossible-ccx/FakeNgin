"""CSV 持久化、缓存、任务互斥及只读离线迁移；无真实模型调用。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from webapp import batches, db, file_store, reports

spec = importlib.util.spec_from_file_location("migrate_risk_storage", ROOT / "scripts" / "migrate_risk_storage.py")
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class FileStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.path_patch = patch.object(db, "DATABASE_DIR", self.directory)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        file_store.clear_cache()
        self.addCleanup(file_store.clear_cache)
        self.state_patch = patch.object(batches, "_initialized_paths", set())
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)

    def job(self, status="queued"):
        job = {
            "id": str(uuid.uuid4()), "status": status, "created_at": "2026-10-04T01:00:00Z",
            "updated_at": "2026-10-04T01:00:00Z", "cancel_requested": False,
            "current_index": None, "total": 1, "completed_count": 0, "saved_count": 0,
            "failed_count": 0, "save_failed_count": 0, "error": None,
            "items": [{"status": "pending", "members": [], "result": None,
                       "record_id": None, "history_error": None}],
        }
        file_store.write_csv(file_store.uuid_path(self.directory / "risk_batches", job["id"]), job)
        return job

    def report(self, message="原始消息"):
        return reports.save_report(message, ["model-1"], {"members": [], "label": "低风险", "level": "low"})

    def test_reports_are_separate_csv_and_never_create_sqlite_or_modify_csv(self):
        csv_path = self.directory / "source.csv"
        csv_path.write_text("content,nature\n原文,待校验\n", encoding="utf-8")
        before = csv_path.read_bytes()
        record = self.report("中文 100% _ 消息")
        path = self.directory / "risk_reports" / (record["id"] + ".csv")
        self.assertTrue(path.is_file())
        with path.open(encoding="utf-8-sig", newline="") as stream:
            stored = list(csv.DictReader(stream))
        self.assertEqual(stored[0], {"path": "", "type": "dict", "value": ""})
        self.assertIn({"path": "/message", "type": "str", "value": "中文 100% _ 消息"}, stored)
        self.assertEqual(reports.get_report(record["id"]), record)
        self.assertEqual(reports.list_reports(query="100% _")["total"], 1)
        self.assertEqual(reports.list_reports(query="missing")["total"], 0)
        self.assertEqual(csv_path.read_bytes(), before)
        self.assertEqual(list(self.directory.rglob("*.sqlite3")), [])

    def test_record_ids_reject_traversal_suffixes_and_unknown_without_writes(self):
        for identifier in ("../source", "../../outside", "C:\\outside", "a.json", "not-a-uuid", None):
            with self.subTest(identifier=identifier):
                self.assertIsNone(reports.get_report(identifier))
                self.assertIsNone(batches.get_batch(identifier))
                self.assertIsNone(batches.cancel_batch(identifier))
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_csv_round_trip_preserves_nested_results_types_and_escaped_fields(self):
        value = {
            "message": '中文逗号, 引号"\n第二行\r\n', "model_ids": ["model-1", "model-4"],
            "result": {"members": [{"score": 0.42, "success": True, "error": None},
                                     {"score": 0.0, "success": False, "error": "未连接"}],
                       "empty_dict": {}, "empty_list": [], "long_integer": 2 ** 80},
            "a/b~c": {"": "空键", "0": "数字键", "~1": None}, "empty": "",
        }
        path = self.directory / "typed.csv"
        file_store.write_csv(path, value)
        file_store.clear_cache()
        self.assertEqual(file_store.read_csv(path), value)
        with path.open(encoding="utf-8-sig", newline="") as stream:
            fields = {row["path"]: row for row in csv.DictReader(stream)}
        self.assertEqual(fields["/result/members/0/score"],
                         {"path": "/result/members/0/score", "type": "float", "value": "0.42"})
        self.assertEqual(fields["/a~1b~0c/"]["value"], "空键")
        self.assertEqual(fields["/result/members"]["value"], "")
        self.assertEqual(list(self.directory.glob("*.json")), [])
        for scalar in (None, True, False, 42, -17, 0.3, "", "单独\r回车", "单独\n换行", "长文" * 100000, [], {}):
            file_store.write_csv(path, scalar)
            actual = file_store.read_csv(path)
            self.assertEqual(actual, scalar)
            self.assertIs(type(actual), type(scalar))

    def test_csv_decoder_rejects_missing_parents_duplicate_paths_and_invalid_types(self):
        path = self.directory / "bad.csv"
        bodies = (
            "wrong,header\n",
            "path,type,value\n",
            "path,type,value\n,dict,\n/missing/child,int,1\n",
            "path,type,value\n,dict,\n/a,int,1\n/a,int,2\n",
            "path,type,value\n,list,\n/1,str,skipped-index\n",
            "path,type,value\n,dict,\n/a~2,str,bad-escape\n",
            "path,type,value\n,dict,\n/a,float,nan\n",
            "path,type,value\n,dict,\n/a,bool,yes\n",
        )
        for body in bodies:
            with self.subTest(body=body):
                path.write_text(body, encoding="utf-8")
                file_store.clear_cache()
                with self.assertRaises(ValueError):
                    file_store.read_csv(path)

    def test_key_paths_are_deterministic_and_do_not_confuse_nested_fields(self):
        first = file_store.key_path(self.directory, ["中文/源.csv", 2, "signature"])
        self.assertEqual(first, file_store.key_path(self.directory, ["中文/源.csv", 2, "signature"]))
        self.assertNotEqual(first, file_store.key_path(self.directory, ["中文", "源.csv/2", "signature"]))
        self.assertEqual(first.suffix, ".csv")
        self.assertEqual(first.parent, self.directory.resolve())

    def test_cached_read_avoids_csv_decode_and_caller_cannot_mutate_cached_value(self):
        path = self.directory / "cached.csv"
        file_store.write_csv(path, {"nested": {"value": 1}})
        file_store.clear_cache()
        with patch.object(file_store, "_decode_csv", wraps=file_store._decode_csv) as load:
            first = file_store.read_csv(path)
            first["nested"]["value"] = 999
            self.assertEqual(file_store.read_csv(path)["nested"]["value"], 1)
            self.assertEqual(load.call_count, 1)

    def test_external_edit_replace_and_delete_invalidate_read_cache(self):
        path = self.directory / "cached.csv"
        file_store.write_csv(path, {"value": 1})
        self.assertEqual(file_store.read_csv(path), {"value": 1})
        path.write_text('path,type,value\n,dict,\n/value,int,22\n', encoding="utf-8")
        self.assertEqual(file_store.read_csv(path), {"value": 22})
        replacement = self.directory / "replacement.tmp"
        replacement.write_text('path,type,value\n,dict,\n/value,int,33\n', encoding="utf-8")
        replacement.replace(path)
        self.assertEqual(file_store.read_csv(path), {"value": 33})
        path.unlink()
        self.assertIsNone(file_store.read_csv(path))

    def test_external_edit_after_atomic_replace_does_not_cache_old_bytes_as_new_version(self):
        path = self.directory / "write-window.csv"
        file_store.write_csv(path, {"value": "cached-original"})
        self.assertEqual(file_store.read_csv(path), {"value": "cached-original"})
        replace = file_store.os.replace

        def external_edit(source, target):
            replace(source, target)
            Path(target).write_text('path,type,value\n,dict,\n/value,str,external-edit\n', encoding="utf-8")

        with patch.object(file_store.os, "replace", side_effect=external_edit):
            file_store.write_csv(path, {"value": "application-write"})
        self.assertEqual(file_store.read_csv(path), {"value": "external-edit"})
        with path.open(encoding="utf-8-sig", newline="") as stream:
            self.assertEqual(file_store.read_csv(path), file_store._decode_csv(stream))

    def test_cache_is_bounded_and_atomic_write_failure_keeps_original(self):
        with patch.object(file_store, "MAX_CACHED_FILES", 3):
            for index in range(7):
                cached_path = self.directory / (str(index) + ".csv")
                file_store.write_csv(cached_path, {"value": index})
                self.assertEqual(file_store.read_csv(cached_path), {"value": index})
            self.assertLessEqual(len(file_store._cache), 3)
            self.assertEqual(len(file_store._cache), 3)
        path = self.directory / "atomic.csv"
        file_store.write_csv(path, {"value": "original"})
        with patch.object(file_store.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                file_store.write_csv(path, {"value": "new"})
        self.assertEqual(file_store.read_csv(path), {"value": "original"})
        self.assertEqual(list(self.directory.glob(".pending-*.tmp")), [])
        with self.assertRaises(ValueError):
            file_store.write_csv(path, {"value": float("nan")})
        self.assertEqual(file_store.read_csv(path), {"value": "original"})

    def test_writer_lock_protects_read_modify_write_across_threads(self):
        path = self.directory / "counter.csv"
        file_store.write_csv(path, {"count": 0})

        def increment(_):
            for _ in range(10):
                with file_store.locked(self.directory):
                    data = file_store.read_csv(path)
                    data["count"] += 1
                    file_store.write_csv(path, data)

        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(increment, range(4)))
        self.assertEqual(file_store.read_csv(path)["count"], 40)

    def test_writer_lock_protects_read_modify_write_across_processes(self):
        path = self.directory / "counter.csv"
        file_store.write_csv(path, {"count": 0})
        script = "\n".join([
            "import sys", "from pathlib import Path",
            "sys.path.insert(0, " + repr(str(ROOT / "src")) + ")",
            "from webapp import file_store", "path = Path(sys.argv[1])",
            "for index in range(12):",
            "    with file_store.locked(path.parent):",
            "        data = file_store.read_csv(path)",
            "        data['count'] += 1", "        file_store.write_csv(path, data)",
        ])
        processes = [subprocess.Popen([sys.executable, "-c", script, str(path)],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
        try:
            for process in processes:
                _, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, stderr.decode("utf-8", errors="replace"))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        self.assertEqual(file_store.read_csv(path)["count"], 24)

    def test_cancel_request_survives_old_worker_snapshot(self):
        job = self.job("running")
        old_snapshot = deepcopy(job)
        cancelled = batches.cancel_batch(job["id"])
        self.assertEqual(cancelled["status"], "cancelling")
        batches._save_job(old_snapshot)
        saved = batches.get_batch(job["id"])
        self.assertTrue(saved["cancel_requested"])
        self.assertEqual(saved["status"], "cancelling")

    def test_queued_cancellation_never_claims_work_and_duplicate_claim_executes_once(self):
        cancelled_job = self.job()
        batches.cancel_batch(cancelled_job["id"])
        with patch.object(batches, "_run_batch") as run:
            batches.run_batch(cancelled_job["id"])
            run.assert_not_called()
        job = self.job()
        barrier = threading.Barrier(2)

        def claim():
            barrier.wait(timeout=5)
            batches.run_batch(job["id"])

        with patch.object(batches, "_run_batch") as run:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(claim) for _ in range(2)]
                for future in futures:
                    future.result(timeout=10)
            self.assertEqual(run.call_count, 1)

    def test_restart_interrupts_unfinished_jobs_and_stale_worker_cannot_revive_them(self):
        active = self.job("running")
        complete = self.job("completed")
        with patch.object(batches, "_start_worker") as start, patch.object(batches, "_run_batch") as run:
            batches.initialize()
            self.assertEqual(batches.get_batch(active["id"])["status"], "interrupted")
            self.assertEqual(batches.get_batch(complete["id"])["status"], "completed")
            batches._save_job(active)
            batches.run_batch(active["id"])
            self.assertEqual(batches.get_batch(active["id"])["status"], "interrupted")
            start.assert_not_called()
            run.assert_not_called()

    def test_reference_files_link_only_exact_content_and_keep_newest_report(self):
        reference = {"file": "sample.csv", "row": 0, "signature": "0123456789abcdef"}
        old = self.report("旧报告")
        new = self.report("新报告")
        batches._link_report(reference, new, self.directory)
        batches._link_report(reference, old, self.directory)
        links = batches.latest_reports([reference, {**reference, "signature": "fedcba9876543210"}])
        key = (reference["file"], reference["row"], reference["signature"])
        self.assertEqual(links[key]["id"], new["id"])
        self.assertEqual(len(links), 1)
        self.assertEqual(len(list((self.directory / "dataset_reports").glob("*.csv"))), 1)

    def seed_sqlite(self):
        report_id, job_id = str(uuid.uuid4()), str(uuid.uuid4())
        with closing(sqlite3.connect(self.directory / "risk_reports.sqlite3")) as connection, connection:
            connection.execute("CREATE TABLE risk_reports (id TEXT PRIMARY KEY, created_at TEXT, message TEXT, "
                               "model_ids_json TEXT, result_json TEXT, prompt_version TEXT)")
            connection.execute("INSERT INTO risk_reports VALUES (?, ?, ?, ?, ?, ?)",
                               (report_id, "2026-10-03T00:00:00Z", "旧中文报告", '["model-1"]',
                                '{"label":"低风险","level":"low","members":[]}', "legacy-prompt"))
        job = {
            "id": job_id, "status": "running", "created_at": "2026-10-03T00:00:00Z",
            "updated_at": "2026-10-03T00:00:01Z", "cancel_requested": False,
            "items": [{"status": "completed", "record_id": report_id, "members": []},
                      {"status": "running", "members": [{"status": "running"}]}],
            "saved_count": 1, "completed_count": 1, "current_index": 1,
        }
        with closing(sqlite3.connect(self.directory / "risk_batches.sqlite3")) as connection, connection:
            connection.execute("CREATE TABLE risk_batches (id TEXT PRIMARY KEY, status TEXT, created_at TEXT, "
                               "updated_at TEXT, cancel_requested INTEGER, payload_json TEXT)")
            connection.execute("CREATE TABLE dataset_reports (file TEXT, row_number INTEGER, signature TEXT, "
                               "report_id TEXT, label TEXT, level TEXT, created_at TEXT)")
            connection.execute("INSERT INTO risk_batches VALUES (?, ?, ?, ?, ?, ?)",
                               (job_id, job["status"], job["created_at"], job["updated_at"], 0, json.dumps(job)))
            connection.execute("INSERT INTO dataset_reports VALUES (?, ?, ?, ?, ?, ?, ?)",
                               ("legacy.csv", 3, "0123456789abcdef", report_id, "低风险", "low", job["created_at"]))
        return report_id, job_id

    def test_offline_migration_is_complete_idempotent_and_keeps_sqlite_bytes(self):
        report_id, job_id = self.seed_sqlite()
        before = {path.name: hashlib.sha256(path.read_bytes()).digest() for path in self.directory.glob("*.sqlite3")}
        preview = migration.migrate_storage(self.directory, dry_run=True)
        self.assertTrue(all(counts["copied"] == 1 for counts in preview.values()))
        self.assertFalse((self.directory / "risk_reports").exists())
        result = migration.migrate_storage(self.directory)
        self.assertTrue(all(counts["copied"] == 1 for counts in result.values()))
        report = reports.get_report(report_id)
        self.assertEqual(report["message"], "旧中文报告")
        self.assertEqual(report["prompt_version"], "legacy-prompt")
        job = batches.get_batch(job_id)
        self.assertEqual(job["status"], "interrupted")
        self.assertEqual(job["items"][0]["record_id"], report_id)
        self.assertEqual(job["items"][1]["members"][0]["status"], "interrupted")
        reference = {"file": "legacy.csv", "row": 3, "signature": "0123456789abcdef"}
        self.assertEqual(next(iter(batches.latest_reports([reference]).values()))["id"], report_id)
        # 新文件的后续更改不会被再次迁移覆盖。
        report["message"] = "迁移后新内容"
        file_store.write_csv(file_store.uuid_path(self.directory / "risk_reports", report_id), report)
        again = migration.migrate_storage(self.directory)
        self.assertTrue(all(counts["copied"] == 0 and counts["skipped"] == 1 for counts in again.values()))
        self.assertEqual(reports.get_report(report_id)["message"], "迁移后新内容")
        after = {path.name: hashlib.sha256(path.read_bytes()).digest() for path in self.directory.glob("*.sqlite3")}
        self.assertEqual(after, before)
        with patch.object(batches, "_run_batch") as run:
            batches.initialize()
            batches.run_batch(job_id)
            run.assert_not_called()

    def test_migration_of_missing_old_databases_does_not_create_any_storage(self):
        result = migration.migrate_storage(self.directory)
        self.assertTrue(all(counts["found"] == 0 for counts in result.values()))
        self.assertEqual(list(self.directory.iterdir()), [])

    def seed_json(self, report_id=None, job_id=None):
        report_id, job_id = report_id or str(uuid.uuid4()), job_id or str(uuid.uuid4())
        report = {
            "id": report_id, "created_at": "2026-10-04T00:00:00Z", "message": "较新 JSON 消息,\n第二行",
            "model_ids": ["model-4"], "prompt_version": "json-prompt",
            "result": {"label": "中风险", "level": "medium", "success_count": 1,
                       "members": [{"id": "model-4", "score": 0.5, "error": None, "status": "completed"}]},
        }
        job = {
            "id": job_id, "status": "running", "created_at": "2026-10-04T00:00:00Z",
            "updated_at": "2026-10-04T00:00:01Z", "current_index": 1, "cancel_requested": True,
            "items": [{"status": "completed", "record_id": report_id, "result": report["result"], "members": []},
                      {"status": "running", "members": [{"status": "running", "score": None}]}],
        }
        reference = {"file": "legacy.csv", "row": 3, "signature": "0123456789abcdef", "id": report_id,
                     "history_url": "/history/" + report_id, "label": "中风险", "level": "medium",
                     "created_at": report["created_at"]}
        for folder, name, record in (("risk_reports", report_id, report), ("risk_batches", job_id, job),
                                     ("dataset_reports", "a" * 64, reference)):
            directory = self.directory / folder
            directory.mkdir(exist_ok=True)
            (directory / (name + ".json")).write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        return report, job, reference

    def test_runtime_ignores_legacy_json_until_explicit_offline_migration(self):
        report, job, reference = self.seed_json()
        before = {str(path): path.read_bytes() for path in self.directory.rglob("*.json")}
        with patch.object(migration.sqlite3, "connect", side_effect=AssertionError("must not use SQL")):
            self.assertIsNone(reports.get_report(report["id"]))
            self.assertIsNone(batches.get_batch(job["id"]))
            self.assertEqual(reports.list_reports()["total"], 0)
            self.assertEqual(batches.list_batches(), [])
            self.assertEqual(batches.latest_reports([reference]), {})
            self.assertEqual({str(path): path.read_bytes() for path in self.directory.rglob("*.json")}, before)
            preview = migration.migrate_storage(self.directory, dry_run=True)
            self.assertTrue(all(counts == {"found": 1, "copied": 1, "skipped": 0} for counts in preview.values()))
            self.assertEqual(list(self.directory.rglob("*.csv")), [])
            migrated = migration.migrate_storage(self.directory)
            self.assertEqual(migrated, preview)
        self.assertEqual(reports.get_report(report["id"]), report)
        restored = batches.get_batch(job["id"])
        self.assertEqual(restored["status"], "interrupted")
        self.assertTrue(restored["cancel_requested"])
        self.assertEqual(restored["items"][0], job["items"][0])
        self.assertEqual(restored["items"][1]["members"][0]["status"], "interrupted")
        self.assertEqual(next(iter(batches.latest_reports([reference]).values()))["label"], "中风险")
        self.assertEqual({str(path): path.read_bytes() for path in self.directory.rglob("*.json")}, before)
        self.assertEqual(list(self.directory.rglob("*.sqlite3")), [])
        again = migration.migrate_storage(self.directory)
        self.assertTrue(all(counts == {"found": 1, "copied": 0, "skipped": 1} for counts in again.values()))

    def test_migration_prefers_newer_json_snapshot_and_dry_run_counts_duplicate_targets_once(self):
        report_id, job_id = self.seed_sqlite()
        report, _, reference = self.seed_json(report_id, job_id)
        before = {str(path): hashlib.sha256(path.read_bytes()).digest()
                  for path in self.directory.rglob("*") if path.is_file()}
        preview = migration.migrate_storage(self.directory, dry_run=True)
        self.assertTrue(all(counts == {"found": 2, "copied": 1, "skipped": 1} for counts in preview.values()))
        migrated = migration.migrate_storage(self.directory)
        self.assertEqual(migrated, preview)
        self.assertEqual(reports.get_report(report_id), report)
        self.assertEqual(next(iter(batches.latest_reports([reference]).values()))["label"], "中风险")
        for path, digest in before.items():
            self.assertEqual(hashlib.sha256(Path(path).read_bytes()).digest(), digest)
        self.assertEqual(len(list(self.directory.rglob("*.csv"))), 3)
        again = migration.migrate_storage(self.directory)
        self.assertTrue(all(counts == {"found": 2, "copied": 0, "skipped": 2} for counts in again.values()))


if __name__ == "__main__":
    unittest.main()
