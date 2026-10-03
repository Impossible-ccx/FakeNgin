# FakeNgin

基于大模型的虚假信息平台

## Setup

python 3.9
需要的库已记录在requirements.txt中。对他们使用conda install应该就能获取全部依赖
已经准备了使用ollama接入的 qwen2.5 7B、deepseek-r1 7B 与 glm4 9B 模型。没有做强制依赖，所以不需要下载。
若要使用它们，需安装 ollama 并拉取对应模型：

```bash
ollama pull qwen2.5:7b
ollama pull deepseek-r1:7b
ollama pull glm4:9b
```

Python 端还需要安装 ollama 客户端库（程序靠它连接本地服务）：

```bash
pip install ollama
```

另外内置了一个中文 RoBERTa 谣言分类器（毫秒级推理，适合批量计算虚假概率）。
使用前安装依赖并训练一次：

```bash
pip install torch transformers
python src/train_rumor_model.py
```

训练数据取自 database/newsdata/output/ 下 csv 的 nature 列（True=谣言，False=非谣言），
产出模型保存在 models/rumor-roberta/。首次训练会通过 hf-mirror.com 下载基座模型。

## Run

```bash
python src/app.py
```

网页开放于 http://127.0.0.1:5000/ 
测试账户为admin，密码为admin

## Model

添加其他模型查看src/checkmodel/下的__init__.py和base.py；
Ollama 系模型可直接复用 src/checkmodel/ollama_base.py 的公共基类。
可选依赖清单见 requirements-optional.txt。

## Check

newscheck脚本将检查现在可用的模型，控制台中进行选择后，对所有数据进行虚假度计算。
