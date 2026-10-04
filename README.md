# FakeNgin

基于本地模型的消息风险分析平台。提供数据展示、风险检测、人工校验与账户登录等功能。

## 安装与运行

建议使用 Python 3.9 或更新版本，并在虚拟环境安装依赖：

```bash
pip install -r requirements.txt
pip install -r requirements-optional.txt
```

`requirements-optional.txt` 含 Ollama、torch、transformers；未安装时对应模型会自动隐藏，不影响其他功能。

另外安装并启动 [Ollama](https://ollama.com/)，下载准备参与检测的模型：

```bash
ollama pull qwen2.5:7b
ollama pull deepseek-r1:7b
ollama pull glm4:9b
python src/app.py
```

网站位于 http://127.0.0.1:5000/ ，风险检测位于 http://127.0.0.1:5000/detect 。
没有模型时页面仍可打开，但检测名单为空；如果之后才启动 Ollama 或下载权重，请重启网站以刷新模型列表。

数据展示与风险检测页无需登录；人工校验页需要登录，本地测试账户为 `admin` / `admin`。检测结果不写入真假标签。

## 批量评分与旧数据

```bash
python src/newscheck.py
```

批量评分统一写入 `fake_probability`。

旧 CSV 读取时缺失字段补空值，写回时按当前列写出。

CLI 仍是单模型批处理，每 20 个成功结果保存一次，单条失败继续；强制中止时最近未保存的结果可能需要重跑。

## 可选的 RoBERTa 训练

RoBERTa 作为独立的真假分类实验；训练产物存在且依赖就绪时，它也会作为可用模型出现在检测页名单中。

```bash
pip install torch transformers
python src/train_rumor_model.py
```

训练数据为 `database/newsdata/output/` 下 CSV；权重输出到 `models/rumor-roberta/`。首次运行下载基座模型，脚本默认通过 `hf-mirror.com`，可使用 `HF_ENDPOINT` 覆盖。仓库不包含训练权重或真实模型评测结论。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试使用模拟模型和隔离的临时数据，覆盖检测流程、边界、非法输出、网页表单/异步路径和存储语义。不依赖真实模型，不代表已验证真实推理效果。

真假数据集标签不应直接当作语言风险等级标签。

测试项目没有被仔细检查过，也不一定真的运行过。出现问题也有可能是测试本身过时了，不要急着改。