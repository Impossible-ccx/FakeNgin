"""批量评分与结果评估脚本。

用法：python src/newscheck.py
启动后选择：
    1  批量评分：检查当前可用模型，在控制台选择后对所有数据评分
    2  仅评估：对现有数据的「虚假概率」与人工校验结果做评估
    0  退出
批量评分完成后会自动对输出数据做一次评估；检测失败的行会跳过并继续，
每 20 行写盘一次。

评估是纯数据层操作：只读取输出数据中的 fake_probability 与 nature 两列，
不关心概率由哪个模型产生，一律把 fake_probability 视为「模型输出的虚假概率」。
"""

import math

import checkmodel
import webapp.newsdata as nd
from checkmodel.base import CheckError

CHECKPOINT_EVERY = 20

# ------------------------------------------------------------- 评估口径
EVAL_SCORE_COLUMN = "fake_probability"
EVAL_THRESHOLD = 50.0
EVAL_SWEEP = (30.0, 40.0, 50.0, 60.0, 70.0)
POSITIVE_NATURE = "虚假"
NEGATIVE_NATURE = "真实"
NEUTRAL_NATURE = "中立"
LABELED_NATURES = {POSITIVE_NATURE, NEGATIVE_NATURE, NEUTRAL_NATURE}


def select_model(models):
    """控制台选择模型，输入 0 返回 None 表示结束。"""
    print("可用模型：")
    print("0: 结束")
    for index, model in enumerate(models, 1):
        print("{}: {}".format(index, model["display_name"]))
    while True:
        target = input("输入目标: ").strip()
        if not target.isdigit():
            print("请输入数字")
            continue
        number = int(target)
        if number == 0:
            return None
        if 1 <= number <= len(models):
            return models[number - 1]
        print("请输入 0-{} 之间的数字".format(len(models)))


def check_table(model, name, override):
    """检测一张表，返回 (成功数, 失败数)。"""
    newsdf = nd.read_table(name)
    done = 0
    failed = 0
    for row in newsdf.itertuples():
        if not override and str(getattr(row, "fake_probability")) != "":
            continue
        try:
            score, _reason = model.check(row.content)
            if isinstance(score, bool):
                raise CheckError("分数无效")
            score = float(score)
            if not math.isfinite(score) or not 0 <= score <= 100:
                raise CheckError("分数必须为 0-100 的有限数值")
        except Exception as exc:
            failed += 1
            print("  第 {} 行检测失败：{}".format(row.Index + 1, exc))
            continue
        newsdf.loc[row.Index, "fake_probability"] = "{:.2f}".format(score)
        done += 1
        if done % CHECKPOINT_EVERY == 0:
            nd.write_table(name, newsdf)
            print("  已检测 {} 条".format(done))
    nd.write_table(name, newsdf)
    return done, failed


# --------------------------------------------------------------- 评估

def collect_eval_samples(names):
    """读取各表输出数据，返回 (样本列表, 无法解析条数)。

    仅保留 fake_probability 非空、且 nature 为 虚假/真实/中立 的行；
    未校验与缺失标注的行跳过。
    """
    samples = []
    invalid = 0
    for name in names:
        df = nd.read_table(name)
        for _, row in df.iterrows():
            raw = str(row.get(EVAL_SCORE_COLUMN, "")).strip()
            nature = str(row.get("nature", "")).strip()
            if not raw or nature not in LABELED_NATURES:
                continue
            try:
                prob = float(raw)
            except (TypeError, ValueError):
                invalid += 1
                continue
            samples.append({"prob": prob, "nature": nature})
    return samples, invalid


def binary_metrics(samples, threshold):
    """仅统计 虚假/真实 样本的二分类指标；正类为 虚假。"""
    tp = fp = tn = fn = 0
    for sample in samples:
        nature = sample["nature"]
        if nature not in (POSITIVE_NATURE, NEGATIVE_NATURE):
            continue
        truth = 1 if nature == POSITIVE_NATURE else 0
        pred = 1 if sample["prob"] >= threshold else 0
        if truth and pred:
            tp += 1
        elif not truth and pred:
            fp += 1
        elif not truth and not pred:
            tn += 1
        else:
            fn += 1

    total = tp + fp + tn + fn
    accuracy = (tp + tn) / total if total else 0.0
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "tp": tp, "fp": fp, "tn": tn, "fn": fn, "total": total,
        "accuracy": accuracy, "precision": precision, "recall": recall, "f1": f1,
    }


def mean_squared_error(samples):
    """虚假=1 / 真实=0 / 中立=0.5，预测为 prob/100；返回 (MSE, 样本数)。"""
    total = 0.0
    count = 0
    for sample in samples:
        nature = sample["nature"]
        if nature == POSITIVE_NATURE:
            truth = 1.0
        elif nature == NEGATIVE_NATURE:
            truth = 0.0
        elif nature == NEUTRAL_NATURE:
            truth = 0.5
        else:
            continue
        total += (sample["prob"] / 100.0 - truth) ** 2
        count += 1
    return (total / count if count else 0.0), count


def print_evaluation(names):
    """汇总评估输出数据的虚假概率与人工校验结果，输出到控制台。"""
    samples, invalid = collect_eval_samples(names)
    positive = sum(1 for s in samples if s["nature"] == POSITIVE_NATURE)
    negative = sum(1 for s in samples if s["nature"] == NEGATIVE_NATURE)
    neutral = sum(1 for s in samples if s["nature"] == NEUTRAL_NATURE)

    print("================ 评估结果 ================")
    print("可评估样本 {} 条：虚假 {} / 真实 {} / 中立 {}".format(
        len(samples), positive, negative, neutral))
    if invalid:
        print("跳过无法解析的虚假概率 {} 条".format(invalid))

    if positive + negative == 0:
        print("没有可用于准确度/F1 的 虚假/真实 样本。")
    else:
        thresholds = [EVAL_THRESHOLD] + [t for t in EVAL_SWEEP if t != EVAL_THRESHOLD]
        for threshold in thresholds:
            metrics = binary_metrics(samples, threshold)
            label = "主阈值" if threshold == EVAL_THRESHOLD else "扫描  "
            print("{} {:.0f}%：准确度 {:.4f} 精确率 {:.4f} 召回率 {:.4f} F1 {:.4f}（n={}）".format(
                label, threshold, metrics["accuracy"], metrics["precision"],
                metrics["recall"], metrics["f1"], metrics["total"]))
            if threshold == EVAL_THRESHOLD:
                print("  混淆矩阵：TP={} FP={} FN={} TN={}（正类=虚假）".format(
                    metrics["tp"], metrics["fp"], metrics["fn"], metrics["tn"]))

    mse_value, mse_count = mean_squared_error(samples)
    print("MSE（虚假=1 / 真实=0 / 中立=0.5，n={}）: {:.4f}".format(mse_count, mse_value))
    print("==========================================")


def main():
    print("请选择操作：")
    print("  1: 批量评分")
    print("  2: 仅评估（现有虚假概率 vs 人工校验）")
    print("  0: 退出")
    choice = input("输入选项: ").strip()

    if choice == "0":
        return
    if choice == "2":
        print_evaluation(nd.list_tables())
        return
    if choice != "1":
        print("无效选项")
        return

    models = checkmodel.get_models()
    if not models:
        print("当前没有可用模型，请检查模型依赖与配置")
        return

    model_info = select_model(models)
    if model_info is None:
        return

    override = input("是否覆盖已有数据？[y/n] ").strip().lower() == "y"
    model = checkmodel.get_model(model_info["id"])

    for table in nd.list_tables():
        done, failed = check_table(model, table, override)
        print("完成 {}：成功 {} 条，失败 {} 条".format(table, done, failed))

    print()
    print_evaluation(nd.list_tables())


if __name__ == "__main__":
    main()
