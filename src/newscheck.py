"""批量虚假度检测脚本。

用法：python src/newscheck.py
检查当前可用模型，控制台中进行选择后，对所有数据进行虚假度计算。
检测失败的行会跳过并继续；每 20 行写盘一次，中断不会丢失已完成的结果。
"""

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
    done = 0
    failed = 0
    for row in newsdf.itertuples():
        if not override and str(row.fake_probability) != "":
            continue
        try:
            probability, _ = model.check(row.content)
        except Exception as exc:
            failed += 1
            print("  第 {} 行检测失败：{}".format(row.Index + 1, exc))
            continue
        probability = max(0.0, min(100.0, float(probability)))
        newsdf.loc[row.Index, "fake_probability"] = "{:.2f}".format(probability)
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
