"""生成实验复现 manifest：把一次训练实验的全部复现信息集中成 JSON。

用法：
    python scripts/experiment_manifest.py --name <实验名>
        --data-dir <隔离数据目录> [--artifact <工件路径>]
        [--split <划分文件>] [--audit <审计报告>]
        [--gap <已知证据缺口说明>]... [--command <复现命令>]...
        [--output <manifest.json 路径>]

记录内容：
- 数据来源与引用、导入与划分统计、数据集指纹、预处理规则版本；
- 主实验/消融的精确命令、超参数、随机种子与线程设置要求；
- 代码提交号与工作区是否干净、Python/NumPy 版本；
- 工件哈希、指标、逐样本预测等证据的存在性（缺失时如实标记，
  不伪造、不自动重跑测试集补证据）。

只读操作：不写数据库、不触发训练；对旧版（无指纹字段）划分文件
重新计算指纹并注明来源。
"""

import argparse
import hashlib
import json
import platform
import sqlite3
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from webapp import newsdata  # noqa: E402

# 真实训练时的线程设置（多线程 BLAS 在微小矩阵上反而极慢且耗时失控）
THREAD_SETTING = {
    "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "note": "必须单线程运行（见 README 本地序列模型章节的实测记录）",
}


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_info():
    """代码提交号与工作区状态（不在 git 仓库时如实标注）。"""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
            cwd=str(PROJECT_ROOT), timeout=10).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True,
            cwd=str(PROJECT_ROOT), timeout=10).stdout.strip()
        return {
            "commit": commit or "unknown",
            "workspace_clean": not status,
            "dirty_entries": len(status.splitlines()) if status else 0,
        }
    except (OSError, subprocess.SubprocessError):
        return {"commit": "unknown", "workspace_clean": None, "dirty_entries": None}


def _dataset_info(data_dir, split):
    """从只读连接计算数据集指纹与统计。"""
    db_path = Path(data_dir) / "fakengin.db"
    conn = sqlite3.connect("file:{}?mode=ro".format(db_path.as_posix()), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, content, nature FROM messages "
            "WHERE nature IN ('虚假', '真实')").fetchall()
        comments = conn.execute("SELECT COUNT(*) AS n FROM comments").fetchone()["n"]
    finally:
        conn.close()
    covered = set(split.get("train") or []) | set(split.get("test") or [])
    items = [{"id": r["id"], "label": 1 if r["nature"] == "虚假" else 0,
              "content": r["content"]} for r in rows if r["id"] in covered]
    return {
        "labeled_messages": len(rows),
        "comments": comments,
        "dataset_fingerprint": newsdata.dataset_fingerprint(items),
        "fingerprint_covers": "划分覆盖的全部带结论消息",
    }


def build_manifest(args):
    data_dir = Path(args.data_dir)
    split_path = Path(args.split) if args.split else \
        data_dir / "weibo16_split.json"
    artifact_path = Path(args.artifact) if args.artifact else \
        data_dir / "tfidf_rnn.json"

    manifest = {
        "experiment": args.name,
        "generated_at": newsdata.db.now_string(),
        "data_dir": str(data_dir),
    }

    # 划分文件
    split = json.loads(split_path.read_text(encoding="utf-8"))
    meta = split.get("meta") if isinstance(split.get("meta"), dict) else {}
    counts = meta.get("counts") if isinstance(meta.get("counts"), dict) else {}
    manifest["split"] = {
        "file": str(split_path),
        "sha256": _sha256(split_path),
        "format": split.get("format"),
        "version": split.get("version"),
        "has_dataset_fingerprint": bool(meta.get("dataset_fingerprint")),
        "importer_version": meta.get("importer_version"),
        "import_rules": meta.get("import_rules"),
        "counts": counts,
    }
    manifest["data"] = _dataset_info(data_dir, split)
    manifest["data"]["source"] = meta.get("source") or "划分文件未注明来源"
    manifest["data"]["strategy"] = meta.get("strategy")

    # 训练工件（可选：仅有划分的实验允许无工件）
    if artifact_path.exists():
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        stats = artifact.get("stats") or {}
        manifest["artifact"] = {
            "file": str(artifact_path),
            "sha256": _sha256(artifact_path),
            "size_bytes": artifact_path.stat().st_size,
            "format": artifact.get("format"),
            "format_version": artifact.get("version"),
            "input_mode": artifact.get(
                "input_mode", "sequence（旧版工件无该字段，按序列口径）"),
            "hyperparameters": {
                "epochs": stats.get("epochs"),
                "hidden_size": artifact.get("hidden_size"),
                "max_sequence_length": artifact.get("max_sequence_length"),
                "vocabulary_size": len(artifact.get("vocabulary") or []),
                "learning_rate": 0.02,
                "batch_size": 16,
                "seed": 20260921,
                "pos_weight": stats.get("pos_weight"),
                "gradient_clip_norm": 5.0,
                "idf_formula": "ln((1+N)/(1+df))+1，L2 归一化，词表按 df 截断",
                "note": "learning_rate/batch/seed/clip 为代码固定缺省值（当时未暴露 CLI 参数）",
            },
            "metrics": {
                "train_samples": stats.get("train_samples"),
                "holdout": stats.get("holdout"),
                "test": stats.get("test"),
            },
            "data_note": stats.get("data_note"),
        }
    else:
        manifest["artifact"] = {
            "file": str(artifact_path),
            "missing": True,
            "note": "工件不存在：证据缺口，如实标记，不以重跑测试集补造",
        }

    # 审计报告（可选）
    if args.audit:
        audit_path = Path(args.audit)
        manifest["audit"] = {
            "file": str(audit_path),
            "sha256": _sha256(audit_path) if audit_path.exists() else None,
        }

    # 复现命令与线程设置
    manifest["reproduction"] = {
        "commands": args.command or [],
        "thread_setting": THREAD_SETTING,
    }

    # 证据缺口（显式登记）
    manifest["evidence_gaps"] = args.gap or []

    # 环境与代码状态
    import numpy
    manifest["environment"] = {
        "python": platform.python_version(),
        "numpy": numpy.__version__,
        "platform": platform.platform(),
    }
    manifest["code"] = _git_info()
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成实验复现 manifest")
    parser.add_argument("--name", required=True, help="实验名称")
    parser.add_argument("--data-dir", required=True, help="隔离数据目录")
    parser.add_argument("--artifact", default="", help="训练工件路径")
    parser.add_argument("--split", default="", help="划分文件路径")
    parser.add_argument("--audit", default="", help="划分审计报告路径")
    parser.add_argument("--command", action="append", default=[],
                        help="复现命令（可多次）")
    parser.add_argument("--gap", action="append", default=[],
                        help="已知证据缺口说明（可多次）")
    parser.add_argument("--output", required=True, help="manifest.json 输出路径")
    args = parser.parse_args(argv)

    try:
        manifest = build_manifest(args)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print("manifest 生成失败：{}".format(exc))
        return 1

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("manifest 已写入：{}".format(output))
    if manifest.get("artifact", {}).get("missing"):
        print("注意：工件缺失，已标记为证据缺口")
    for gap in manifest["evidence_gaps"]:
        print("证据缺口：{}".format(gap))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
