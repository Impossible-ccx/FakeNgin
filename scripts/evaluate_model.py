"""模型评测框架：对带标签的评测集运行模型，输出指标报告。

用法：
    python scripts/evaluate_model.py <评测集csv> [--model <model_id>] [--output <报告路径>]

评测集 CSV 列（表头必须一致）：
    content,label
    - label: fake / real（二分类；其余取值报错拒跑，防止标签口径混乱）
    - 标签必须来自可靠来源（官方辟谣、权威媒体核实等），并在报告 meta 中注明来源。

指标：Precision / Recall / F1（fake 为正类）、混淆矩阵、平均与最大延迟、失败率。
阈值：默认 50；可用 --threshold 调整，调参只用验证集，不得用测试集。

边界声明：本脚本是可复现的评测工具；未运行于真实标注评测集之前，
其任何输出都不构成模型准确率评测结论。当前项目算法路线（PDF 的
TF-IDF/RNN 路线 vs 现有 Qwen 方案）尚待教师确认，确认前不宣称任何评测达标。
"""

import argparse
import csv
import json
import statistics
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

VALID_LABELS = {"fake", "real"}


def load_dataset(path):
    rows = []
    errors = []
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        header = [name.strip() for name in (reader.fieldnames or [])]
        if "content" not in header or "label" not in header:
            raise ValueError("评测集必须包含 content 与 label 两列")
        for line_no, raw in enumerate(reader, start=2):
            content = (raw.get("content") or "").strip()
            label = (raw.get("label") or "").strip().lower()
            if not content:
                errors.append({"row": line_no, "error": "空正文"})
                continue
            if label not in VALID_LABELS:
                errors.append({"row": line_no,
                               "error": "标签必须是 fake/real，当前:{}".format(label)})
                continue
            rows.append({"content": content, "label": label})
    if errors:
        raise ValueError("评测集存在问题（拒绝运行）：{}".format(
            "; ".join("第{}行 {}".format(e["row"], e["error"]) for e in errors[:5])))
    if not rows:
        raise ValueError("评测集为空")
    return rows


def run_model(model, rows, threshold):
    """逐条推理，返回预测与延迟列表。"""
    predictions = []
    latencies = []
    failures = []
    for index, row in enumerate(rows):
        started = time.time()
        try:
            probability, reason = model.check(row["content"])
            probability = float(probability)
            if not 0 <= probability <= 100:
                raise ValueError("评分超出 0-100")
            predicted = "fake" if probability >= threshold else "real"
            predictions.append({
                "index": index, "label": row["label"], "predicted": predicted,
                "probability": probability, "reason": str(reason)[:200],
            })
        except Exception as exc:
            predictions.append({
                "index": index, "label": row["label"], "predicted": "error",
                "probability": None, "reason": "{}: {}".format(type(exc).__name__, exc),
            })
            failures.append(index)
        latencies.append(time.time() - started)
    return predictions, latencies, failures


def compute_metrics(predictions, latencies, failures):
    # 失败样本不计入混淆矩阵，单独以 failure_rate 报告
    valid = [p for p in predictions if p["predicted"] != "error"]
    tp = sum(1 for p in valid if p["label"] == "fake" and p["predicted"] == "fake")
    fp = sum(1 for p in valid if p["label"] == "real" and p["predicted"] == "fake")
    fn = sum(1 for p in valid if p["label"] == "fake" and p["predicted"] == "real")
    tn = sum(1 for p in valid if p["label"] == "real" and p["predicted"] == "real")
    error_count = len(failures)
    total = len(predictions)

    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    ok_latencies = [latencies[i] for i in range(total) if i not in set(failures)]
    return {
        "total": total,
        "confusion_matrix": {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
                             "error": error_count},
        "precision_fake": round(precision, 4),
        "recall_fake": round(recall, 4),
        "f1_fake": round(f1, 4),
        "failure_rate": round(error_count / total, 4) if total else 0.0,
        "latency_ms": {
            "mean": round(statistics.mean(latencies) * 1000) if latencies else 0,
            "max": round(max(latencies) * 1000) if latencies else 0,
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", help="评测集 CSV（content,label）")
    parser.add_argument("--model", default="", help="模型 id，默认取当前可用模型")
    parser.add_argument("--threshold", type=float, default=50.0,
                        help="fake 判定阈值（0-100，默认 50）")
    parser.add_argument("--output", default="", help="报告输出路径（JSON），默认打印")
    args = parser.parse_args()

    import checkmodel

    checkmodel.reprobe()
    if args.model:
        try:
            model = checkmodel.get_model(args.model)
        except KeyError:
            print("模型不可用：{}".format(args.model))
            return 1
    else:
        models = checkmodel.get_models()
        if not models:
            print("没有可用模型")
            return 1
        model = checkmodel.get_model(models[0]["id"])

    rows = load_dataset(args.dataset)
    print("评测集：{} 条（fake {} / real {}）".format(
        len(rows),
        sum(1 for r in rows if r["label"] == "fake"),
        sum(1 for r in rows if r["label"] == "real")))
    print("模型：{}（{}） 阈值：{}".format(
        model.name, getattr(model, "display_name", ""), args.threshold))
    print("开始推理……")

    predictions, latencies, failures = run_model(model, rows, args.threshold)
    metrics = compute_metrics(predictions, latencies, failures)
    report = {
        "dataset": str(args.dataset),
        "model": {"id": model.name, "display_name": getattr(model, "display_name", "")},
        "threshold": args.threshold,
        "metrics": metrics,
        "predictions": predictions,
        "note": "风险评分非校准概率；未在真实标注评测集上运行前不构成准确率结论。",
    }

    report_json = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(report_json, encoding="utf-8")
        print("报告已写入：{}".format(args.output))
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
