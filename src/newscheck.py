import checkmodel
import webapp.newsdata as nd
import pandas as pd
import sys

if __name__ == "__main__":
    models = checkmodel.get_models()

    print("可用模型：")
    print("0: 结束")
    cnt = 1
    for model in models:
        print(str(cnt)+": "+ model["display_name"])
        cnt += 1
    print("输入目标:")
    target = input()
    if(target == 0):
        sys.exit(0)
    if(int(target) > len(models)):
        sys.exit(0)
    print("是否覆盖已有数据？[y/n]")
    override_str = input()
    override = False
    if(override_str == 'y'):
        override = True
    else:
        override = False
    model = checkmodel.get_model(models[int(target)-1]["id"])

    tables = nd.list_tables()
    for table in tables:
        newsdf = nd.read_table(table)
        cnt = 0
        for row in newsdf.itertuples():
            if(not override and not row.fake_probability == ''):
                continue
            res = model.check(row.content)
            newsdf.loc[row.Index, 'fake_probability'] = res[0]
            cnt += 1
            if(cnt % 20 == 0):
                print("finished 20 rows")
        nd._write_path(nd.NEWSDATA_DIR/table, newsdf)
        print("finished " + table)



