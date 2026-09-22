"""训练本地序列模型（TF-IDF + 评论时间序列 + RNN）并写入工件。

数据来源：库内人工校验结论为"虚假/真实"的消息及其评论回复树
（对应 PDF 模型处理流程：TF-IDF 特征提取 → 评论按时间排序 →
特征矩阵 → RNN）。训练只用本地数据，不访问外部模型服务。

用法：
    python scripts/train_tfidf_rnn.py [--data-note 数据来源说明]
        [--epochs 200] [--hidden 24] [--holdout 0.2]
        [--seed 20260921] [--max-features 5000]
        [--split-file <weibo16_split.json>]

如实说明：
- 默认按类别分层留出 20% 仅用于报告，超参数取固定缺省值、不在留出集上调参；
- 指定 --split-file 时（如 Weibo16 近重复分组划分），训练只用训练折，
  测试折指标仅作最终报告，不参与训练与调参；
- 指标只反映训练所用的本地数据集，训练数据为演示/合成样例时不代表
  真实场景效果。
"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from checkmodel import tfidf_rnn  # noqa: E402
from webapp import newsdata  # noqa: E402

LABEL_NATURES = ("虚假", "真实")
MIN_TOTAL_SAMPLES = 8
MIN_CLASS_SAMPLES = 2

USAGE = (
    "用法：python scripts/train_tfidf_rnn.py [--data-note 数据来源说明] "
    "[--epochs 200] [--hidden 24] [--holdout 0.2] [--seed 20260921] "
    "[--max-features 5000] [--split-file <划分文件>]"
)


def _parse_args(argv):
    options = {
        "data_note": "",
        "epochs": tfidf_rnn.DEFAULT_EPOCHS,
        "hidden": tfidf_rnn.DEFAULT_HIDDEN_SIZE,
        "holdout": tfidf_rnn.DEFAULT_HOLDOUT_FRACTION,
        "seed": tfidf_rnn.DEFAULT_SEED,
        "max_features": tfidf_rnn.DEFAULT_MAX_FEATURES,
        "split_file": "",
    }
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--data-note", "--epochs", "--hidden", "--holdout",
                   "--seed", "--max-features", "--split-file"):
            if i + 1 >= len(argv):
                raise ValueError("{} 需要一个参数值".format(arg))
            value = argv[i + 1]
            if arg == "--data-note":
                options["data_note"] = value
            elif arg == "--epochs":
                options["epochs"] = max(1, int(value))
            elif arg == "--hidden":
                options["hidden"] = max(2, int(value))
            elif arg == "--holdout":
                options["holdout"] = min(0.5, max(0.0, float(value)))
            elif arg == "--seed":
                options["seed"] = int(value)
            elif arg == "--max-features":
                options["max_features"] = max(10, int(value))
            elif arg == "--split-file":
                options["split_file"] = value
            i += 2
        else:
            raise ValueError("无法识别的参数：{}".format(arg))
    return options


def _load_labeled_rows():
    """库内带人工结论的消息（含评论）。"""
    rows = []
    for row in newsdata.load_all():
        if row["nature"] in LABEL_NATURES:
            comments = newsdata.list_comments(row["id"])
            rows.append((row, tfidf_rnn.build_sequence(row["content"], comments)))
    return rows


def _to_samples(pairs):
    return [(texts, 1 if row["nature"] == "虚假" else 0) for row, texts in pairs]


def _print_metrics(name, metrics):
    if not metrics:
        print("{}：无样本（该类别样本不足，未留出）".format(name))
        return
    c = metrics["confusion"]
    print("{}：accuracy={:.3f} precision={:.3f} recall={:.3f} f1={:.3f}".format(
        name, metrics["accuracy"], metrics["precision"], metrics["recall"],
        metrics["f1"]))
    print("  混淆矩阵（正类=谣言）：TP={} FP={} FN={} TN={}".format(
        c["tp"], c["fp"], c["fn"], c["tn"]))


def main():
    try:
        options = _parse_args(sys.argv[1:])
    except ValueError as exc:
        print(str(exc))
        print(USAGE)
        return 1

    pairs = _load_labeled_rows()
    samples = _to_samples(pairs)
    test_samples = None

    if options["split_file"]:
        try:
            with open(options["split_file"], "r", encoding="utf-8") as fh:
                split = json.load(fh)
            train_ids = set(split.get("train") or [])
            test_ids = set(split.get("test") or [])
        except (OSError, ValueError) as exc:
            print("划分文件无法读取：{}".format(exc))
            return 1
        covered = train_ids | test_ids
        kept = [(row, texts) for row, texts in pairs if row["id"] in covered]
        if len(kept) != len(pairs):
            print("注意：{} 条带结论消息不在划分文件覆盖范围内，已从本次训练排除".format(
                len(pairs) - len(kept)))
        samples = _to_samples([(row, texts) for row, texts in kept
                               if row["id"] in train_ids])
        test_samples = _to_samples([(row, texts) for row, texts in kept
                                    if row["id"] in test_ids])
        if not options["data_note"]:
            meta = split.get("meta") or {}
            options["data_note"] = str(meta.get("source") or "划分文件未注明来源")
        print("外部划分：训练 {} 条 / 测试 {} 条（测试折不参与训练与调参）".format(
            len(samples), len(test_samples)))

    positives = sum(1 for _, label in samples if label == 1)
    negatives = len(samples) - positives
    if len(samples) < MIN_TOTAL_SAMPLES or min(positives, negatives) < MIN_CLASS_SAMPLES:
        print("训练样本不足：当前人工判定消息 {} 条（虚假 {} / 真实 {}），"
              "至少需要 {} 条且每类 ≥ {} 条。".format(
                  len(samples), positives, negatives,
                  MIN_TOTAL_SAMPLES, MIN_CLASS_SAMPLES))
        print("请先在人工校验页标注更多消息，或导入带标注的训练样例"
              "（samples/demo_training_messages.csv 与 samples/demo_comments.csv，"
              "或 scripts/import_weibo16.py 导入 Weibo16）。")
        return 1

    holdout_fraction = 0.0 if test_samples is not None else options["holdout"]
    print("开始训练：样本 {} 条（谣言 {} / 真实 {}），epochs={}，隐藏层 {}，"
          "留出比例 {}".format(
              len(samples), positives, negatives, options["epochs"],
              options["hidden"],
              0 if test_samples is not None else options["holdout"]))

    def _progress(epoch, avg_loss):
        # 每 10 个 epoch 打一行进度，长训练可观测
        if epoch % 10 == 0 or epoch == options["epochs"]:
            print("  epoch {}/{}，平均损失 {:.4f}".format(
                epoch, options["epochs"], avg_loss), flush=True)

    try:
        artifact = tfidf_rnn.train_tfidf_rnn(
            samples,
            hidden_size=options["hidden"],
            epochs=options["epochs"],
            holdout_fraction=holdout_fraction,
            seed=options["seed"],
            max_features=options["max_features"],
            data_note=options["data_note"],
            test_samples=test_samples,
            progress=_progress,
        )
    except ValueError as exc:
        print("训练失败：{}".format(exc))
        return 1

    stats = artifact["stats"]
    print("训练完成：训练折 {} 条，最终训练损失 {:.4f}".format(
        stats["train_samples"], stats["final_train_loss"]))
    _print_metrics("训练集", stats["train"])
    if stats.get("holdout"):
        _print_metrics("留出集（分层抽样，仅报告）", stats["holdout"])
    if stats.get("test"):
        print("测试集为外部划分（近重复分组防泄漏，未参与训练与调参）")
        _print_metrics("测试集", stats["test"])

    path = tfidf_rnn.artifact_path()
    tfidf_rnn.save_artifact(artifact, path)
    print("数据说明：{}".format(stats["data_note"]))
    print("注意：以上指标仅反映训练所用的本地数据集，不代表真实场景效果；"
          "评分未经概率校准，界面作为风险评分展示。")
    print("工件已写入：{}".format(path))
    print("下一步：在检测/复核页选择“本地序列模型（TF-IDF+RNN）”，"
          "或点击检测页的“重新探测”。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
