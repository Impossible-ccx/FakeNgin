# FakeNgin

虚假信息（谣言）检测平台：消息导入/录入/受控采集 → 模型风险评分（本地 TF-IDF+RNN 序列模型或远程大模型）→ 人工校验 → 数据展示与检索。

## 环境要求

- Python 3.9 及以上（实测 3.13 可运行）
- 依赖见 `requirements.txt`：

```bash
pip install -r requirements.txt
```

- jieba 用于搜索分词；Pandas 用于数据读写；Flask 为 Web 框架。
- 检测模型为可选项（见下文“模型配置”），不配置时其余页面仍可使用。

## 启动

```bash
python src/app.py
```

网页开放于 http://127.0.0.1:5000/

- 业务数据存放在 SQLite：`database/fakengin.db`（首次启动自动创建；可用环境变量 `FAKENGIN_DATA_DIR` 指向其他目录）。
- 旧版 CSV 数据迁移到 SQLite：

```bash
python scripts/migrate_csv_to_sqlite.py
```

  迁移先备份原文件到 `database_backup_<时间戳>/` 再导入，不删除、不修改原 CSV；
  旧概率保留并标记为“历史导入（来源未知）”，不会伪装成新模型检测结果。
- CSV 导入／导出：

```bash
python scripts/import_csv.py <csv文件>   # 校验失败不导入；重复正文跳过
python scripts/import_csv.py --comments <评论csv>   # 导入评论回复树（见“演示数据”）
python scripts/export_csv.py <输出文件>
```

- 首次初始化不设固定口令：自动生成随机管理员口令并写入
  `database/admin_initial_password.txt`（仅管理员可读，登录后请修改并删除该文件）；
  也可在首次启动前用环境变量 `FAKENGIN_ADMIN_PASSWORD` 指定初始口令。
- 密码一律以哈希保存；写接口有 CSRF 校验与角色权限（admin/reviewer 可写，viewer 只读）。

## 安全说明

- CSRF：全部 POST 表单携带会话令牌，缺失或不匹配返回 400。
- 权限：写操作（增删改、检测、审核、采集）要求 admin/reviewer 角色，服务端强制校验；
  检测页“保存并提交复核”在模型调用前校验角色，匿名与 viewer 勾选保存返回 403。
- 限流：登录连续失败 5 次（同账户 + 来源 IP，5 分钟窗口）暂时锁定；
  公开检测入口每 IP 每分钟最多 10 次模型调用，超出返回 429。
- 会话：登录凭证 3 天过期，过期自动清理；HTTPS 部署时设置
  `FAKENGIN_COOKIE_SECURE=1`（同时覆盖 Flask 会话与登录 Cookie 的 Secure 标记）。
- 日志：不记录密码、Cookie、密钥；检测失败日志仅含任务 ID 与错误类型。
- 调试：`src/app.py` 默认关闭 Flask debug，仅 `FLASK_DEBUG=1` 时开启。
- Cookie：httponly + SameSite=Lax。

## 运维（修改密码 / 备份恢复）

- 修改密码：登录后右上角“修改密码”（`/account/password`），需验证原密码。
- 忘记密码：`python scripts/reset_admin_password.py <用户名>`（交互输入）或加
  `--generate` 生成随机密码并打印一次。
- 备份：`python scripts/backup_db.py [目录]` 使用 SQLite backup API 生成在线一致快照
  （应用运行中也可执行），默认写到 `database_backup_<时间戳>/`。
- 恢复：停止应用 → 删除数据目录中 `fakengin.db`、`fakengin.db-wal`、`fakengin.db-shm`
  → 复制备份的 `fakengin.db` 回去 → 重启（搜索索引按数据版本自动重建）。

## 页面

| 页面 | 说明 |
|---|---|
| `/data` 谣言数据展示 | 消息列表、分页与关键词搜索、词云 |
| `/detect` 谣言检测系统 | 输入文本做模型风险评分（0–100，越高越可疑），可重新探测模型状态 |
| `/verify` 人工校验系统 | 登录后对消息增删改与人工校验 |
| `/collect` 在线采集 | 仅 admin：查看受控消息来源、触发一次性采集、查看运行记录 |

## 演示数据

`samples/` 下提供两组明确标注的合成演示数据（非真实事件）：

```bash
# 8 条未校验消息：用于演示检测 → 人工复核流程
python scripts/import_csv.py samples/demo_messages.csv

# 24 条带人工结论（12 虚假 / 12 真实）+ 31 条评论回复树：用于训练本地序列模型
python scripts/import_csv.py samples/demo_training_messages.csv
python scripts/import_csv.py --comments samples/demo_comments.csv
```

评论 CSV 列为 `ref, message_content, parent_ref, content, publish_time`：
`message_content` 须与已导入消息正文完全一致，`parent_ref` 引用文件中更早的
评论行构成回复树；校验失败整文件不导入，重复导入自动跳过。

## 在线采集（受控消息来源）

管理员登录后访问 `/collect` 可对服务端登记的消息来源执行一次性采集；
采集到的消息以“未校验”进入检测与人工复核流程。

当前登记的来源（`src/webapp/collect_sources.py`，新增来源需先完成调研与许可确认）：

| 来源 | 端点 | 说明 |
|---|---|---|
| Solidot（奇客资讯） | `https://www.solidot.org/index.rss` | 官方 RSS 2.0，robots.txt 放行，无需登录，大陆直连可达，纯文本全文 |
| IT之家 | `https://www.ithome.com/rss/` | 官方 RSS 2.0，robots.txt 放行 `/rss/`，正文 HTML 自动转纯文本 |

安全边界（服务端强制，不提供任意 URL 入口）：

- 仅 HTTPS、仅 allowlist 精确 URL；重定向默认拒绝；URL 带用户名密码直接拒绝。
- 域名解析后校验全部地址并绑定连接（防 DNS 重绑定）：回环、私网、链路本地
  （含云元数据）、组播、保留地址与 IPv4 映射 IPv6 一律拒绝。
- 限流与预算：同来源请求起始间隔 ≥ 5 秒；单次任务最多 10 个请求、最长 120 秒；
  每来源每次最多接收 20 条。
- 体积与编码：传输与解压后各限 1 MiB（流式累计实际字节）；仅接受适配器支持的
  XML/JSON 内容类型；不支持的压缩编码拒绝。
- 重试：网络错误与 5xx 最多重试 1 次；429 尊重 Retry-After；403 本轮停用不重试。
- 解析防护：拒绝 DTD/实体定义；HTML 一律转纯文本（剥离脚本与标签）；链接仅保留
  http/https；超长正文显式截断标记；来源正文一律视为不可信数据，不执行其中指令。
- ETag/Last-Modified 条件请求，304 不重复解析入库；（来源 ID + 外部条目 ID）
  幂等去重 + 正文内容级去重，重复执行不重复建消息。
- 每次运行记录请求数、获取/新增/重复/拒绝/导入条数与错误摘要（`collection_runs` 表），
  失败不产生假消息、不破坏已有数据。
- 定时采集默认关闭，仅提供一次性入口；“导入后加入检测队列”需显式勾选，
  避免一次采集触发大量模型请求。

注意：采集到的新闻文本不等于谣言，只是送入模型检测与人工复核的素材；
人工结论仍以复核记录为准。

## Docker 部署

前提：宿主机已安装 Docker 与 Docker Compose。

```bash
# 1. 准备环境变量（模型配置、会话密钥等；也可直接改根目录 .env）
cp .env.example .env

# 2. 多 worker 部署必须固定会话密钥，生成后填入 .env 的 FLASK_SECRET_KEY
python -c "import secrets; print(secrets.token_hex(32))"

# 3. 构建并启动（数据持久化在宿主机 ./database 目录）
docker compose up -d --build
```

- 访问 http://127.0.0.1:5000/ ；`docker compose logs -f web` 查看日志；`docker compose down` 停止。
- 首次启动生成随机管理员口令文件 `database/admin_initial_password.txt`（0600）；
  也可在 `.env` 中设置 `FAKENGIN_ADMIN_PASSWORD`。
- `.env` 中的模型配置经环境变量注入容器，密钥不会进入镜像；容器内可直接访问
  局域网模型服务。
- SQLite 数据、搜索索引、口令文件与本地模型工件（`tfidf_rnn.json`）都在
  `./database` 挂载卷中，容器重启/重建不丢失；宿主机可直接备份整个目录。
- 挂载目录属主需与容器运行用户一致（UID 1000）：属主不匹配时启动会以
  “unable to open database file” 明确失败，用 `chown -R 1000:1000 database` 修正；
  不要用 root 运行或 chmod 777 规避。
- 多进程任务策略：gunicorn 多 worker 下检测队列按“原子认领 + worker 心跳”安全并发，
  已死进程遗留的 running 任务在心跳超时（默认 300 秒，`FAKENGIN_WORKER_STALE_SECONDS`
  可调）后自动标记为中断、可重试；活跃任务不受影响。
- 健康检查：`docker compose ps` 显示 healthy 即正常。
- HTTPS 部署建议在反代（如 Nginx/Caddy）后终止 TLS，并设置
  `FAKENGIN_COOKIE_SECURE=1`。

## 模型配置

检测页对文本输出“风险评分”与理由；评分仅供参考，最终结论以人工校验为准。支持本地与远程两类模型。

### 本地序列模型（TF-IDF + RNN，课程 PDF 原路线）

按课程 PDF 的模型处理流程实现：TF-IDF 文本特征提取 → 评论按时间排序 →
组成特征矩阵 → 输入预训练 RNN 输出风险评分。纯 numpy 实现，不依赖外部服务。

训练数据来自库内人工校验结论为“虚假/真实”的消息及其评论回复树：

```bash
python scripts/train_tfidf_rnn.py --data-note "数据来源说明"
```

- 样本不足（少于 8 条或任一类少于 2 条）时拒绝训练并提示先标注；
  可先导入 `samples/demo_training_messages.csv` 与 `samples/demo_comments.csv`。
- 训练打印训练/留出集指标；留出集按类别分层抽样，仅用于报告。
  **指标只反映训练所用的本地数据集**，用合成演示数据训练时不代表真实效果。
- 工件写入 `database/tfidf_rnn.json`（与数据目录同置，Docker 部署时随挂载卷
  持久化），可用 `FAKENGIN_LOCAL_MODEL_PATH` 另行指定；重新训练后无需重启，
  模型按文件变化自动重载。
- 检测时输入为“正文 + 该消息评论按时间排序”的完整序列（PDF 回复树路线）；
  检测页自由文本无评论数据，序列只含正文。检测与复核页均可选择该模型。
- 输出为相对风险评分（0–100），未经概率校准，理由中标注训练样本数与数据说明。

#### 真实数据评测（Weibo16）

`scripts/import_weibo16.py` 把公开标注数据集 Weibo16（Ma et al., IJCAI 2016；
4,664 个微博事件：谣言 2,313 / 非谣言 2,351，转发含父帖/时间/正文，学术研究用途）
导入**隔离数据目录**，并生成"近重复分组"划分文件（清洗后正文前 50 字符为组键、
整组同侧、按类别分层），防止同一谣言的变体跨训练/测试泄漏：

```bash
python scripts/import_weibo16.py <rumdect解压目录或zip> --data-dir <隔离目录>
FAKENGIN_DATA_DIR=<隔离目录> python scripts/train_tfidf_rnn.py \
    --split-file <隔离目录>/weibo16_split.json --data-note "Weibo16（Ma et al. IJCAI 2016）"
```

必须指定隔离目录（或预先设置 `FAKENGIN_DATA_DIR`），不写入业务库；目标库非空时
拒绝导入。真实数据训练耗时约十几分钟，建议固定单线程
（`OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1`）——微小矩阵上多线程 BLAS 反而更慢。

2026-09-22 实测（训练 3,731 / 测试 932，超参数预先固定、未在测试集上调参）：
完整序列路线（正文+评论）测试集 F1 0.676；同划分仅正文消融测试集 F1 0.871——
在该轻量 RNN 的末步读出下，真实转发文本（//@ 链、闲聊）会稀释源帖信号，
评论序列未带来增益。详细数字与分析见 `deliverables/系统概述.md` 与开发记录；
后续改进（池化/注意力读出、更短序列窗口等）应在训练折内的验证集上选择。

### 远程模型（Chat Completions 兼容接口）

将 `.env.example` 复制为项目根目录的 `.env`，填写：

| 环境变量 | 用途 |
|---|---|
| `MODEL_API_BASE_URL` | 兼容接口地址，包含 `/v1`，不要包含 `/chat/completions` |
| `MODEL_API_KEY` | 接口密钥，仅放在环境变量或本地 `.env` |
| `MODEL_API_MODEL` | 服务端提供的模型名称 |
| `MODEL_API_TIMEOUT` | 请求超时秒数，默认 60 |
| `MODEL_API_MAX_TOKENS` | 单次推理输出上限，默认 1024；理由较长被截断时调大 |
| `FLASK_SECRET_KEY` | 固定的随机会话密钥；未设置时每个进程随机生成，重启会使 Flask 会话失效 |

应用自动加载 `.env`，已设置的进程环境变量优先。配置变更后重启应用。
接口使用 `POST /v1/chat/completions` 和 Bearer 认证；模型需返回 JSON 风险评分及理由。
配置完整后检测页会列出远程模型，这仅代表配置有效，实际连通性由检测请求验证。
检测页提供“重新探测模型”按钮：模型服务恢复或配置修改后无需重启应用。
`.env` 已被 Git 忽略，切勿将真实密钥填写到 `.env.example` 或提交到仓库。

### Ollama（可选）

原有 Ollama 接口保留，可通过 `OLLAMA_HOST`、`OLLAMA_MODEL` 和 `OLLAMA_TIMEOUT` 配置；
其中地址由 Ollama 客户端读取。未设置时使用本地服务、`qwen2.5:7b` 和 60 秒超时。
需要安装 `ollama` Python 包并启动本地 Ollama 服务，未安装时该模型自动不可用。

### 新增模型

查看 `src/checkmodel/` 下的 `__init__.py` 和 `base.py`：新建 py 文件继承 `CheckModel`，
通过模块级 `MODEL_CLASS` 暴露并在 `MODEL_MODULES` 登记，实现 `detect()`、`unavailable_reason()` 与 `check()`；
需要利用评论回复树序列的模型可再覆写 `check_sequence(source_text, comments)`（默认退化为单文本检测）。

## 测试

```bash
PYTHONPATH=src python -m pytest tests/ -q
```

全部测试使用临时数据目录，不读写真实业务数据。
