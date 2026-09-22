"""训练本地模型（TF-IDF + 评论时间序列 + RNN）并写入工件。

数据来源：库内人工校验结论为"虚假/真实"的消息及其评论回复树
（对应 PDF 模型处理流程：TF-IDF 特征提取 → 评论按时间排序 →
特征矩阵 → RNN）。训练只用本地数据，不访问外部模型服务。

用法：
    python scripts/train_tfidf_rnn.py [--data-note 数据来源说明]
        [--epochs 200] [--hidden 24] [--holdout 0.2]
        [--seed 20260921] [--max-features 5000]
        [--split-file <weibo16_split.json>]
        [--input sequence|content]

--input sequence：正文 + 评论时间序列（PDF 完整路线，默认）；
--input content：仅正文（消融实验口径，推理时同样忽略评论）。

如实说明：
- 默认按类别分层留出 20% 仅用于报告，超参数取固定缺省值、不在留出集上调参；
- 指定 --split-file 时（如 Weibo16 近重复分组划分），训练只用训练折，
  测试折指标仅作最终报告，不参与训练与调参；词表与 IDF 只在训练侧拟合；
- 划分文件经过严格校验：结构、ID 交集/重复/未知、类别分布与数据集指纹，
  版本 1 的旧文件无指纹字段时跳过身份校验并明确提示；
- 指标只反映训练所用的本地数据集，训练数据为演示/合成样例时不代表
  真实场景效果。
"""

import hashlib
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
SUPPORTED_SPLIT_VERSIONS = (1, 2)

USAGE = (
    "用法：python scripts/train_tfidf_rnn.py [--data-note 数据来源说明] "
    "[--epochs 200] [--hidden 24] [--holdout 0.2] [--seed 20260921] "
    "[--max-features 5000] [--split-file <划分文件>] "
    "[--input sequence|content]"
)


class SplitFileError(ValueError):
    """划分文件不合规（结构、ID、类别或身份校验失败）。"""


def _parse_args(argv):
    options = {
        "data_note": "",
        "epochs": tfidf_rnn.DEFAULT_EPOCHS,
        "hidden": tfidf_rnn.DEFAULT_HIDDEN_SIZE,
        "holdout": tfidf_rnn.DEFAULT_HOLDOUT_FRACTION,
        "seed": tfidf_rnn.DEFAULT_SEED,
        "max_features": tfidf_rnn.DEFAULT_MAX_FEATURES,
        "split_file": "",
        "input": "sequence",
    }
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--data-note", "--epochs", "--hidden", "--holdout",
                   "--seed", "--max-features", "--split-file", "--input"):
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
            elif arg == "--input":
                if value not in ("sequence", "content"):
                    raise ValueError("--input 只支持 sequence 或 content")
                options["input"] = value
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


def _to_samples(pairs, input_mode):
    if input_mode == "content":
        return [([row["content"]], 1 if row["nature"] == "虚假" else 0)
                for row, _texts in pairs]
    return [(texts, 1 if row["nature"] == "虚假" else 0) for row, texts in pairs]


def _check_id_list(value, name):
    """一侧 ID 列表的结构校验：int（非 bool）、无重复、非空。"""
    if not isinstance(value, list) or not value:
        raise SplitFileError("划分文件的 {} 侧必须是非空列表".format(name))
    seen = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise SplitFileError(
                "{} 侧存在非整数 ID：{!r}".format(name, item))
        if item in seen:
            raise SplitFileError("{} 侧存在重复 ID：{}".format(name, item))
        seen.add(item)
    return value


def _load_split_file(path):
    """读取划分文件并做结构校验；身份与覆盖校验见 _validate_split。"""
    try:
        raw = Path(path).read_bytes()
        split = json.loads(raw)
    except OSError as exc:
        raise SplitFileError("划分文件无法读取：{}".format(exc))
    except ValueError as exc:
        raise SplitFileError("划分文件不是有效 JSON：{}".format(exc))
    if not isinstance(split, dict) or split.get("format") != "fakengin-split":
        raise SplitFileError("划分文件格式不正确（需要 fakengin-split）")
    version = split.get("version")
    if version not in SUPPORTED_SPLIT_VERSIONS:
        raise SplitFileError(
            "划分文件版本不支持：{}（支持：{}）".format(
                version, "、".join(map(str, SUPPORTED_SPLIT_VERSIONS))))
    train_ids = _check_id_list(split.get("train"), "train")
    test_ids = _check_id_list(split.get("test"), "test")
    intersection = set(train_ids) & set(test_ids)
    if intersection:
        raise SplitFileError(
            "train 与 test 存在 {} 个交集 ID（例如 {}），拒绝训练".format(
                len(intersection), sorted(intersection)[:5]))
    split_sha256 = hashlib.sha256(raw).hexdigest()
    return split, train_ids, test_ids, split_sha256


def _validate_split(split, train_ids, test_ids, labeled_rows, input_label):
    """身份与分布校验：未知 ID、数据集指纹、类别分布与覆盖报告。

    返回 (train_pairs, test_pairs, report_lines, split_info)。
    覆盖策略：库内带结论但不在划分内的消息从本次训练与测试中排除，
    并明确报告数量——不静默丢弃后宣称完整复现。
    """
    labeled = {row["id"]: (row, texts) for row, texts in labeled_rows}
    unknown = [mid for mid in train_ids + test_ids if mid not in labeled]
    if unknown:
        raise SplitFileError(
            "划分文件包含 {} 个当前库中不存在的消息 ID（例如 {}）。"
            "自增 ID 相同不代表同一数据集，请核对划分文件与数据目录".format(
                len(unknown), unknown[:5]))

    meta = split.get("meta") if isinstance(split.get("meta"), dict) else {}
    report = []
    covered = set(train_ids) | set(test_ids)
    uncovered = sorted(set(labeled) - covered)
    if uncovered:
        report.append(
            "覆盖说明：库内 {} 条带结论消息不在划分文件覆盖范围内，"
            "本次训练与测试均不包含（划分共覆盖 {} 条）".format(
                len(uncovered), len(covered)))

    # 数据集指纹（版本 2 起）：错库或内容被改动时拒绝
    fingerprint = newsdata.dataset_fingerprint(
        [{"id": mid, "label": 1 if labeled[mid][0]["nature"] == "虚假" else 0,
          "content": labeled[mid][0]["content"]} for mid in covered])
    if split.get("version") >= 2:
        expected = meta.get("dataset_fingerprint")
        if not expected:
            report.append("注意：划分文件为版本 2 但缺少数据集指纹，跳过身份校验")
        elif expected != fingerprint:
            raise SplitFileError(
                "数据集指纹不匹配：划分文件记录 {}，当前库计算 {}。"
                "消息内容或标注与生成划分时不一致，拒绝训练".format(
                    expected[:16], fingerprint[:16]))
    else:
        report.append(
            "注意：划分文件为版本 1（无数据集指纹），已跳过身份校验；"
            "建议用新版导入脚本重新生成划分文件")

    train_pairs = [labeled[mid] for mid in train_ids]
    test_pairs = [labeled[mid] for mid in test_ids]
    for side_name, pairs in (("训练", train_pairs), ("测试", test_pairs)):
        labels = {row["nature"] for row, _ in pairs}
        missing = [n for n in LABEL_NATURES if n not in labels]
        if missing:
            raise SplitFileError(
                "{}侧缺少类别 {}，无法完成二分类训练/评测".format(
                    side_name, "、".join(missing)))

    label_counts = {
        "train": {"虚假": sum(1 for r, _ in train_pairs if r["nature"] == "虚假"),
                  "真实": sum(1 for r, _ in train_pairs if r["nature"] == "真实")},
        "test": {"虚假": sum(1 for r, _ in test_pairs if r["nature"] == "虚假"),
                 "真实": sum(1 for r, _ in test_pairs if r["nature"] == "真实")},
    }
    split_info = {
        "file": str(split.get("_path", "")),
        "sha256": split.get("_sha256", ""),
        "version": split.get("version"),
        "dataset_fingerprint": fingerprint,
        "train_count": len(train_ids),
        "test_count": len(test_ids),
        "label_counts": label_counts,
        "uncovered_labeled_rows": len(uncovered),
        "input_mode": input_label,
    }
    return train_pairs, test_pairs, report, split_info


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
    samples = _to_samples(pairs, options["input"])
    test_samples = None
    split_info = None

    if options["split_file"]:
        try:
            split, train_ids, test_ids, split_sha256 = \
                _load_split_file(options["split_file"])
            split["_path"] = str(options["split_file"])
            split["_sha256"] = split_sha256
            train_pairs, test_pairs, report, split_info = _validate_split(
                split, train_ids, test_ids, pairs, options["input"])
        except SplitFileError as exc:
            print("划分文件校验失败：{}".format(exc))
            return 1
        for line in report:
            print(line)
        samples = _to_samples(train_pairs, options["input"])
        test_samples = _to_samples(test_pairs, options["input"])
        if not options["data_note"]:
            options["data_note"] = str(
                (split.get("meta") or {}).get("source") or "划分文件未注明来源")
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
    mode_label = ("正文＋评论序列（PDF 完整路线）" if options["input"] == "sequence"
                  else "仅正文（消融口径）")
    print("输入模式：{}".format(mode_label))
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
            input_mode=options["input"],
            progress=_progress,
        )
    except ValueError as exc:
        print("训练失败：{}".format(exc))
        return 1

    if split_info is not None:
        artifact["stats"]["split"] = split_info

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
