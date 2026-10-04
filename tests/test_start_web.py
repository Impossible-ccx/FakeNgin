"""便携启动器路径及环境准备验证；不安装依赖、不运行真实服务。"""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "start_web.py"
spec = importlib.util.spec_from_file_location("start_web", SCRIPT)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)

READY = {"python": [3, 12, 10], "missing": []}
MISSING = {"python": [3, 12, 10], "missing": ["flask", "pandas", "jieba", "ollama"]}


class StartWebTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="网页启动 路径 ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "项目 空格目录"
        self.root.mkdir()

    def test_existing_current_dependencies_reuse_python_without_new_environment(self):
        with patch.object(launcher, "_probe", return_value=READY), patch.object(launcher.subprocess, "run") as run:
            plan = launcher.environment_plan(self.root)
            self.assertTrue(plan["ready"])
            self.assertFalse(plan["create_venv"])
            self.assertFalse(plan["install"])
            self.assertEqual(launcher._prepare(self.root, plan), Path(sys.executable))
            run.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_existing_repo_environment_is_reused_when_current_python_lacks_dependencies(self):
        local_python = launcher._venv_python(self.root)
        local_python.parent.mkdir(parents=True)
        local_python.write_bytes(b"placeholder")
        with patch.object(launcher, "_probe", side_effect=[MISSING, READY]), patch.object(launcher.subprocess, "run") as run:
            plan = launcher.environment_plan(self.root)
            self.assertTrue(plan["ready"])
            self.assertFalse(plan["create_venv"])
            self.assertEqual(launcher._prepare(self.root, plan), local_python)
            run.assert_not_called()

    def test_missing_environment_only_plans_basic_dependency_install_in_local_venv(self):
        local_python = launcher._venv_python(self.root)
        plan = {"ready": False, "create_venv": True}
        commands = []

        def fake_run(command, **kwargs):
            commands.append((command, kwargs))
            if command[1:3] == ["-m", "venv"]:
                local_python.parent.mkdir(parents=True)
                local_python.write_bytes(b"placeholder")
            return Mock(returncode=0)

        with patch.object(launcher, "_probe", side_effect=[MISSING, READY]), \
                patch.object(launcher.subprocess, "run", side_effect=fake_run):
            self.assertEqual(launcher._prepare(self.root, plan), local_python)
        self.assertEqual(commands[0][0], [sys.executable, "-m", "venv", str(self.root / ".venv")])
        self.assertEqual(commands[1][0][-2:], ["-r", str(self.root / "requirements.txt")])
        self.assertTrue(all(command[1]["cwd"] == str(self.root) for command in commands))
        flattened = " ".join(str(argument) for command, _ in commands for argument in command)
        for forbidden in ("torch", "transformers", "requirements-optional", "ollama pull"):
            self.assertNotIn(forbidden, flattened)

    def test_dependency_install_failure_is_readable_and_does_not_start_app(self):
        local_python = launcher._venv_python(self.root)
        local_python.parent.mkdir(parents=True)
        local_python.write_bytes(b"placeholder")
        with patch.object(launcher, "_probe", return_value=MISSING), \
                patch.object(launcher.subprocess, "run", return_value=Mock(returncode=1)), \
                patch.object(launcher, "_serve") as serve:
            with self.assertRaisesRegex(launcher.StartupError, "安装失败"):
                launcher._prepare(self.root, {"ready": False, "create_venv": False})
            serve.assert_not_called()
        self.assertFalse((self.root / "database").exists())

    def test_no_install_and_unsupported_python_fail_without_creating_venv(self):
        with patch.object(launcher.subprocess, "run") as run:
            with self.assertRaisesRegex(launcher.StartupError, "依赖缺失或无法导入"):
                launcher._prepare(self.root, {"ready": False}, no_install=True)
            with patch.object(launcher.sys, "version_info", (3, 9, 0)):
                with self.assertRaisesRegex(launcher.StartupError, "3.10"):
                    launcher._prepare(self.root, {"ready": False, "create_venv": True})
            run.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_broken_current_dependencies_fall_back_to_existing_ready_repo_environment(self):
        local_python = launcher._venv_python(self.root)
        local_python.parent.mkdir(parents=True)
        local_python.write_bytes(b"placeholder")
        broken = {"python": [3, 12, 10], "missing": [], "broken": {"pandas": "ImportError: DLL load failed"}}
        with patch.object(launcher, "_probe", side_effect=[broken, READY]), \
                patch.object(launcher.subprocess, "run") as run:
            plan = launcher.environment_plan(self.root)
            self.assertTrue(plan["ready"])
            self.assertEqual(launcher._prepare(self.root, plan), local_python)
            run.assert_not_called()

    def test_probe_detects_import_failure_and_never_writes_bytecode(self):
        # 包可被 find_spec 发现但不能导入，模拟常见 DLL / 二进制版本冲突。
        modules = self.root / "broken packages"
        modules.mkdir()
        for name in launcher.DEPENDENCIES:
            content = "raise ImportError('DLL load failed')\n" if name == "pandas" else "READY = True\n"
            (modules / (name + ".py")).write_text(content, encoding="utf-8")
        environment = {**os.environ, "PYTHONPATH": str(modules)}
        with patch.dict(os.environ, environment, clear=True):
            state = launcher._probe(sys.executable)
        self.assertEqual(state["missing"], [])
        self.assertEqual(state["broken"], {"pandas": "ImportError: DLL load failed"})
        self.assertFalse(launcher._ready(state))
        self.assertFalse(list(modules.rglob("*.pyc")))

    def test_failed_probe_is_reported_without_crashing_the_read_only_check(self):
        with patch.object(launcher, "_probe", return_value=None), \
                patch.object(launcher, "PROJECT_ROOT", self.root):
            self.assertEqual(launcher.main(["--check"]), 1)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_occupied_port_is_reported_without_starting_or_preparing_app(self):
        # 只占用系统分配的临时端口，不操作用户的 5000 服务。
        with launcher.socket.socket(launcher.socket.AF_INET, launcher.socket.SOCK_STREAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen(1)
            port = occupied.getsockname()[1]
            with patch.object(launcher, "environment_plan", return_value={"ready": True}), \
                    patch.object(launcher, "_prepare") as prepare, patch.object(launcher, "_serve") as serve:
                self.assertEqual(launcher.main(["--no-browser", "--port", str(port)]), 1)
                prepare.assert_not_called()
                serve.assert_not_called()

    def copied_script(self):
        scripts = self.root / "scripts"
        scripts.mkdir()
        copied = scripts / "start_web.py"
        shutil.copyfile(SCRIPT, copied)
        return copied

    def test_check_from_other_working_directory_with_chinese_and_spaces_has_no_side_effects(self):
        copied = self.copied_script()
        result = subprocess.run(
            [sys.executable, str(copied), "--check", "--json"], cwd=self.temporary.name,
            capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(Path(data["project_root"]), self.root)
        self.assertTrue(data["ready"])
        self.assertFalse(data["create_venv"])
        self.assertEqual([path.relative_to(self.root) for path in self.root.rglob("*")],
                         [Path("scripts"), Path("scripts/start_web.py")])

    def test_missing_dependencies_check_and_dry_run_never_install_or_create_storage(self):
        copied = self.copied_script()
        for option, exit_code in (("--check", 1), ("--dry-run", 0)):
            with self.subTest(option=option):
                result = subprocess.run(
                    [sys.executable, "-S", str(copied), option, "--json"], cwd=self.temporary.name,
                    capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
                )
                self.assertEqual(result.returncode, exit_code, result.stderr)
                data = json.loads(result.stdout)
                self.assertFalse(data["ready"])
                self.assertTrue(data["create_venv"])
                self.assertTrue(data["install"])
                self.assertEqual(set(data["state"]["missing"]), set(launcher.DEPENDENCIES))
        self.assertFalse((self.root / ".venv").exists())
        self.assertFalse((self.root / "database").exists())

    def test_server_starts_without_debug_reload_or_browser_when_requested(self):
        options = launcher._parse_args(["--no-browser", "--port", "5123"])
        app = Mock()
        factory = Mock(return_value=app)
        # 模拟应用模块，根本不创建数据库或探测模型。
        with patch.object(sys, "path", list(sys.path)), \
                patch.dict(sys.modules, {"webapp": Mock(create_app=factory)}), \
                patch.object(launcher.threading, "Thread") as thread:
            launcher._serve(self.root, options)
            thread.assert_not_called()
        app.run.assert_called_once_with(host="127.0.0.1", port=5123, debug=False, use_reloader=False)

    @unittest.skipUnless(os.name == "nt", "Windows batch entry")
    def test_windows_batch_check_resolves_root_from_another_working_directory(self):
        self.copied_script()
        shutil.copyfile(ROOT / "run.bat", self.root / "run.bat")
        # 固定为已具备基础依赖的当前解释器，避免真实环境准备。
        fake_bin = self.root / "命令 目录"
        fake_bin.mkdir()
        wrapper = fake_bin / "py.bat"
        wrapper.write_bytes(('@echo off\r\n"' + sys.executable + '" "%~2" %3 %4\r\n').encode("utf-8"))
        environment = {**os.environ, "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}
        result = subprocess.run(
            ["cmd", "/d", "/c", str(self.root / "run.bat"), "--check", "--json"],
            cwd=self.temporary.name, env=environment, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        data = json.loads(result.stdout)
        self.assertEqual(Path(data["project_root"]), self.root)
        self.assertTrue(data["ready"])
        self.assertFalse((self.root / ".venv").exists())
        self.assertFalse((self.root / "database").exists())

    @unittest.skipUnless(os.name == "nt", "Windows batch entry")
    def test_windows_batch_shim_failure_returns_to_readable_launcher_message(self):
        shutil.copyfile(ROOT / "run.bat", self.root / "run.bat")
        fake_bin = self.root / "命令 目录"
        fake_bin.mkdir()
        (fake_bin / "py.bat").write_bytes(b"@echo off\r\nexit /b 7\r\n")
        environment = {**os.environ, "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")}
        result = subprocess.run(
            ["cmd", "/d", "/c", str(self.root / "run.bat")], cwd=self.temporary.name,
            env=environment, input="\n", capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        self.assertIn("网站未启动", result.stdout)
        self.assertFalse((self.root / "database").exists())


if __name__ == "__main__":
    unittest.main()
