"""批量评分脚本；语言风险分与真假分类概率分别存储。

用法：python src/newscheck.py
检查当前可用模型，控制台中进行选择后，对所有数据评分。
检测失败的行会跳过并继续；每 20 行写盘一次。
"""

import math

import checkmodel
import webapp.newsdata as nd
from checkmodel.base import CheckError

CHECKPOINT_EVERY = 20


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
    is_risk = getattr(model, "score_kind", "probability") == "risk"
    score_column = "risk_score" if is_risk else "fake_probability"
    done = 0
    failed = 0
    for row in newsdf.itertuples():
        if not override and str(getattr(row, score_column)) != "":
            continue
        try:
            score, reason = model.check(row.content)
            if isinstance(score, bool):
                raise CheckError("分数无效")
            score = float(score)
            if not math.isfinite(score) or not 0 <= score <= 100:
                raise CheckError("分数必须为 0-100 的有限数值")
        except Exception as exc:
            failed += 1
            print("  第 {} 行检测失败：{}".format(row.Index + 1, exc))
            continue
        newsdf.loc[row.Index, score_column] = "{:.2f}".format(score)
        if is_risk:
            newsdf.loc[row.Index, "risk_model"] = model.name
            newsdf.loc[row.Index, "risk_reason"] = reason
            newsdf.loc[row.Index, "risk_prompt_version"] = getattr(model, "prompt_version", "")
        done += 1
        if done % CHECKPOINT_EVERY == 0:
            nd.write_table(name, newsdf)
            print("  已检测 {} 条".format(done))
    nd.write_table(name, newsdf)
    return done, failed


def main():
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


if __name__ == "__main__":
    main()
