# FakeNgin

基于大模型的虚假信息平台

## Setup

python 3.9
需要的库已记录在requirements.txt中。对他们使用conda install应该就能获取全部依赖
已经准备了一个使用ollama接入的，qwen2.5 7B模型。没有做强制依赖，所以不需要下载。

## Run

```bash
python src/app.py
```

网页开放于 http://127.0.0.1:5000/ 

## Model

添加其他模型查看src/checkmodel/下的__init__.py和base.py
