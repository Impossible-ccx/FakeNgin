"""从任意工作目录启动网站，只自动安装基础网页依赖，不下载模型。

python scripts/start_web.py --check       只检查，不写文件、不安装、不启动
python scripts/start_web.py --dry-run     预览将使用的环境和准备步骤
python scripts/start_web.py --no-browser  启动网站，不自动打开浏览器
"""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import webbrowser

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEPENDENCIES = ("flask", "pandas", "jieba", "ollama")
MIN_PYTHON = (3, 10)
MAX_PYTHON = (3, 14)
_PROBE = "\n".join([
    "import importlib.util, json, sys",
    "modules = " + repr(DEPENDENCIES),
    "print(json.dumps({'python': list(sys.version_info[:3]), 'missing': "
    "[name for name in modules if importlib.util.find_spec(name) is None]}))",
])


class StartupError(RuntimeError):
    pass


def _supported(state):
    return MIN_PYTHON <= tuple(state["python"][:2]) <= MAX_PYTHON


def _ready(state):
    return state is not None and _supported(state) and not state["missing"]


def _probe(executable):
    if Path(executable).resolve() == Path(sys.executable).resolve():
        return {
            "python": list(sys.version_info[:3]),
            "missing": [name for name in DEPENDENCIES if importlib.util.find_spec(name) is None],
        }
    try:
        process = subprocess.run(
            [str(executable), "-c", _PROBE], capture_output=True, text=True,
            encoding="utf-8", timeout=30, check=False,
        )
        return json.loads(process.stdout) if process.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def _venv_python(root):
    return root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def environment_plan(root):
    """仅检查解释器和包位置；不导入应用，故不会创建用户数据。"""
    root = Path(root).resolve()
    current = _probe(sys.executable)
    if _ready(current):
        return {"ready": True, "executable": sys.executable, "state": current,
                "create_venv": False, "install": False}
    local_python = _venv_python(root)
    local = _probe(local_python) if local_python.is_file() else None
    return {
        "ready": _ready(local), "executable": str(local_python),
        "state": local or current, "create_venv": not (root / ".venv").exists(),
        "install": not _ready(local),
    }


def _prepare(root, plan, no_install=False):
    if plan["ready"]:
        return Path(plan["executable"])
    if no_install:
        raise StartupError("缺少基础依赖；允许自动安装后重试，或先按 requirements.txt 安装。")
    if not _supported(_probe(sys.executable)):
        raise StartupError("请使用 Python 3.10 至 3.14，再重新运行启动器。")
    local_python = _venv_python(root)
    if plan["create_venv"]:
        print("正在为本项目创建 .venv 环境……", flush=True)
        result = subprocess.run([sys.executable, "-m", "venv", str(root / ".venv")], cwd=str(root), check=False)
        if result.returncode != 0:
            raise StartupError("虚拟环境创建失败；请确认 Python 包含 venv 和 ensurepip。")
    if not local_python.is_file():
        raise StartupError("已有 .venv 环境不完整；请将其改名后重新运行，或使用完整的 Python 环境。")
    local_state = _probe(local_python)
    if local_state is None or not _supported(local_state):
        raise StartupError("已有 .venv 无法运行或 Python 版本不受支持；请将其改名后重新创建。")
    print("正在安装基础网页依赖和模型连接客户端；不会下载模型权重……", flush=True)
    result = subprocess.run([
        str(local_python), "-m", "pip", "install", "--disable-pip-version-check",
        "--only-binary=pandas,numpy", "-r", str(root / "requirements.txt"),
    ], cwd=str(root), check=False)
    if result.returncode != 0:
        raise StartupError("基础依赖安装失败；请检查网络连接和 pip 输出，再重新运行。")
    if not _ready(_probe(local_python)):
        raise StartupError("安装后仍缺少基础依赖；请检查上方安装输出。")
    return local_python


def _port_available(host, port):
    try:
        with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind((host, port))
    except OSError as exc:
        raise StartupError("无法使用 {}:{}；端口可能已占用，可加 --port 5001 换一个端口。".format(host, port)) from exc


def _browser_when_ready(url, host, port):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                webbrowser.open(url)
                return
        except OSError:
            time.sleep(0.2)


def _serve(root, options):
    sys.path.insert(0, str(root / "src"))
    from webapp import create_app

    app = create_app()
    browser_host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(options.host, options.host)
    url_host = "[{}]".format(browser_host) if ":" in browser_host else browser_host
    url = "http://{}:{}/".format(url_host, options.port)
    print("网站入口：" + url, flush=True)
    print("按 Ctrl+C 停止服务。", flush=True)
    if not options.no_browser:
        threading.Thread(target=_browser_when_ready, args=(url, browser_host, options.port), daemon=True).start()
    app.run(host=options.host, port=options.port, debug=False, use_reloader=False)


def _parse_args(argv):
    parser = argparse.ArgumentParser(description="启动 FakeNgin 网页，按需准备基础依赖，不下载模型")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-install", action="store_true", help="缺少依赖时直接提示，不自动安装")
    inspection = parser.add_mutually_exclusive_group()
    inspection.add_argument("--check", action="store_true", help="只检查环境，不安装、不启动、不创建数据")
    inspection.add_argument("--dry-run", action="store_true", help="只预览环境准备步骤，不修改任何文件")
    parser.add_argument("--json", action="store_true", help="检查或预览时输出 JSON")
    options = parser.parse_args(argv)
    if not 1 <= options.port <= 65535:
        parser.error("端口必须在 1 至 65535 之间")
    return options


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    options = _parse_args(argv)
    root = PROJECT_ROOT
    plan = environment_plan(root)
    if options.check or options.dry_run:
        if options.json:
            print(json.dumps({"project_root": str(root), **plan}, ensure_ascii=False))
        else:
            print("项目目录：" + str(root))
            print("Python：" + plan["executable"])
            if not _supported(plan["state"]):
                print("请使用 Python 3.10 至 3.14。")
            else:
                print("环境已就绪。" if plan["ready"] else "需要准备基础依赖：" + ", ".join(plan["state"]["missing"]))
            if plan["create_venv"] and not plan["ready"]:
                print("正常启动时将在本项目创建 .venv；本次检查不会创建。")
        return 0 if options.dry_run or plan["ready"] else 1
    try:
        _port_available(options.host, options.port)
        executable = _prepare(root, plan, no_install=options.no_install)
        if executable.resolve() != Path(sys.executable).resolve():
            # 使用参数列表启动，中文、空格目录不需要 shell 或手工拼接引号。
            return subprocess.run([str(executable), str(Path(__file__).resolve()), *argv, "--no-install"],
                                  cwd=str(root), check=False).returncode
        _serve(root, options)
    except KeyboardInterrupt:
        return 0
    except (OSError, StartupError, ImportError) as exc:
        print("启动失败：" + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
