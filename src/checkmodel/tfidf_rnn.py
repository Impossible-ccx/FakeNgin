"""本地序列检测模型：TF-IDF 特征 + 评论时间序列 + RNN。

对应课程 PDF"服务器检测结果呈现模块"的模型处理流程：
  步骤 1：使用 TF-IDF 进行文本特征提取；
  步骤 2：按时间顺序对评论排序（捕捉评论传播的时间动态性）；
  步骤 3：根据序列化后的评论组建特征矩阵；
  步骤 4：将特征矩阵输入预训练好的 RNN，输出谣言概率值。

如实说明：
- RNN 为纯 numpy 实现的 Elman 循环网络（tanh 隐层 + sigmoid 输出），
  不依赖深度学习框架，适合课程数据规模；
- 训练数据来自库内人工校验结论为"虚假/真实"的消息及其评论回复树，
  由 scripts/train_tfidf_rnn.py 离线训练并保存工件（JSON）；
- 输出是相对风险评分（0-100），未经概率校准，不解释为真实概率；
- 工件路径可用 FAKENGIN_LOCAL_MODEL_PATH 指定，默认与数据目录同置
  （database/tfidf_rnn.json），Docker 部署时随挂载卷持久化。
"""

import hashlib
import json
import math
import os
import threading
from datetime import datetime
from pathlib import Path

import numpy as np

try:
    import jieba
except ImportError:  # pragma: no cover - jieba 是锁定依赖，仅在异常环境缺失
    jieba = None

from .base import CheckError, CheckModel
from .base import build_sequence as base_build_sequence

ARTIFACT_FORMAT = "tfidf-rnn"
ARTIFACT_VERSION = 1
INPUT_MODES = ("sequence", "content")

DEFAULT_MAX_SEQUENCE_LENGTH = 32
DEFAULT_MAX_FEATURES = 5000
DEFAULT_HIDDEN_SIZE = 24
DEFAULT_EPOCHS = 200
DEFAULT_BATCH_SIZE = 16
DEFAULT_LEARNING_RATE = 0.02
DEFAULT_HOLDOUT_FRACTION = 0.2
DEFAULT_SEED = 20260921

GRADIENT_CLIP_NORM = 5.0
EPS = 1e-7

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def artifact_path():
    """模型工件路径：FAKENGIN_LOCAL_MODEL_PATH 优先，默认与数据目录同置。"""
    configured = os.getenv("FAKENGIN_LOCAL_MODEL_PATH", "").strip()
    if configured:
        return Path(configured)
    data_dir = os.getenv("FAKENGIN_DATA_DIR", "").strip()
    base = Path(data_dir) if data_dir else PROJECT_ROOT / "database"
    return base / "tfidf_rnn.json"


# ------------------------------------------------------------ 文本预处理

def tokenize(text):
    """jieba 分词：过滤纯符号片段，英文统一小写。"""
    text = str(text or "")
    if jieba is None:  # pragma: no cover
        return [ch.lower() for ch in text if ch.isalnum()]
    tokens = []
    for raw in jieba.lcut(text):
        token = raw.strip().lower()
        if token and any(ch.isalnum() for ch in token):
            tokens.append(token)
    return tokens


class TfidfVectorizer:
    """平滑 TF-IDF：idf = ln((1+N)/(1+df)) + 1，行向量 L2 归一化。

    与常见实现（sklearn smooth_idf）公式一致；词表按文档频率截断，
    并列时按词序排序保证确定性。
    """

    def __init__(self, max_features=DEFAULT_MAX_FEATURES):
        self.max_features = max(1, int(max_features))
        self.vocabulary = {}
        self.idf = np.zeros(0, dtype=np.float64)

    def fit(self, texts):
        df = {}
        for text in texts:
            for token in set(tokenize(text)):
                df[token] = df.get(token, 0) + 1
        ranked = sorted(df.items(), key=lambda kv: (-kv[1], kv[0]))
        kept = ranked[: self.max_features]
        self.vocabulary = {token: idx for idx, (token, _) in enumerate(kept)}
        n_docs = len(texts)
        self.idf = np.array(
            [math.log((1.0 + n_docs) / (1.0 + df[token])) + 1.0 for token, _ in kept],
            dtype=np.float64,
        )
        return self

    def transform(self, texts):
        """文本列表 -> (n, V) 矩阵；训练后未见过的词被忽略。"""
        vocab_size = len(self.vocabulary)
        matrix = np.zeros((len(texts), vocab_size), dtype=np.float64)
        for row, text in enumerate(texts):
            counts = {}
            for token in tokenize(text):
                idx = self.vocabulary.get(token)
                if idx is not None:
                    counts[idx] = counts.get(idx, 0) + 1
            for idx, count in counts.items():
                matrix[row, idx] = count * self.idf[idx]
            norm = math.sqrt(float(np.dot(matrix[row], matrix[row])))
            if norm > 0:
                matrix[row] /= norm
        return matrix


def build_sequence(source_text, comments, max_length=DEFAULT_MAX_SEQUENCE_LENGTH):
    """[源消息正文] + 评论按 (publish_time, id) 升序，截断到 max_length。

    规则实现统一放在 checkmodel.base（模型与检测层的输入指纹共用），
    此处保留同名入口兼容既有调用。截断保留最早的评论（谣言的早期
    传播与辟谣信号多出现在早期）。
    """
    return base_build_sequence(source_text, comments, max_length)


# ------------------------------------------------------------ RNN 网络

class RnnNetwork:
    """Elman RNN：h_t = tanh(x_t·Wxh + h_(t-1)·Whh + bh)，p = sigmoid(h_T·Why + by)。

    权重均为 numpy 数组；forward 只读，加载后的实例可多线程并发推理。
    """

    def __init__(self, vocab_size, hidden_size, seed):
        rng = np.random.default_rng(seed)
        scale = 1.0 / math.sqrt(hidden_size)
        self.hidden_size = hidden_size
        self.Wxh = rng.uniform(-scale, scale, (vocab_size, hidden_size))
        self.Whh = rng.uniform(-scale, scale, (hidden_size, hidden_size))
        self.bh = np.zeros(hidden_size)
        self.Why = rng.uniform(-scale, scale, hidden_size)
        self.by = np.zeros(1)

    def forward(self, matrix):
        """matrix: (T, V) 特征矩阵。返回 (hs (T,H), p 标量)。"""
        steps = matrix.shape[0]
        hs = np.zeros((steps, self.hidden_size))
        prev = np.zeros(self.hidden_size)
        for t in range(steps):
            prev = np.tanh(matrix[t] @ self.Wxh + prev @ self.Whh + self.bh)
            hs[t] = prev
        logit = float(np.clip(hs[-1] @ self.Why + self.by[0], -50.0, 50.0))
        return hs, 1.0 / (1.0 + math.exp(-logit))

    def backward(self, matrix, hs, p, label, pos_weight):
        """单样本 BPTT。返回 (grads dict, loss)。"""
        loss = -(
            pos_weight * label * math.log(max(p, EPS))
            + (1 - label) * math.log(max(1 - p, EPS))
        )
        delta_out = (1 - label) * p - pos_weight * label * (1 - p)
        grads = {
            "Wxh": np.zeros_like(self.Wxh),
            "Whh": np.zeros_like(self.Whh),
            "bh": np.zeros_like(self.bh),
            "Why": np.zeros_like(self.Why),
            "by": np.zeros_like(self.by),
        }
        grads["Why"] += delta_out * hs[-1]
        grads["by"] += delta_out
        dh = delta_out * self.Why
        for t in range(matrix.shape[0] - 1, -1, -1):
            dz = dh * (1.0 - hs[t] * hs[t])
            grads["bh"] += dz
            grads["Wxh"] += np.outer(matrix[t], dz)
            h_prev = hs[t - 1] if t > 0 else np.zeros(self.hidden_size)
            grads["Whh"] += np.outer(h_prev, dz)
            if t > 0:
                dh = dz @ self.Whh.T
        return grads, loss

    def params(self):
        return [self.Wxh, self.Whh, self.bh, self.Why, self.by]


class AdamOptimizer:
    """标准 Adam；参数列表按引用更新。"""

    def __init__(self, params, learning_rate):
        self.params = params
        self.lr = learning_rate
        self.t = 0
        self.m = [np.zeros_like(p) for p in params]
        self.v = [np.zeros_like(p) for p in params]

    def step(self, grads):
        self.t += 1
        beta1, beta2, eps = 0.9, 0.999, 1e-8
        for i, (param, grad) in enumerate(zip(self.params, grads)):
            self.m[i] = beta1 * self.m[i] + (1 - beta1) * grad
            self.v[i] = beta2 * self.v[i] + (1 - beta2) * grad * grad
            m_hat = self.m[i] / (1 - beta1 ** self.t)
            v_hat = self.v[i] / (1 - beta2 ** self.t)
            param -= self.lr * m_hat / (np.sqrt(v_hat) + eps)


def _zero_grads(net):
    return {
        "Wxh": np.zeros_like(net.Wxh),
        "Whh": np.zeros_like(net.Whh),
        "bh": np.zeros_like(net.bh),
        "Why": np.zeros_like(net.Why),
        "by": np.zeros_like(net.by),
    }


def _clip_grads(grads, norm_limit):
    total = math.sqrt(sum(float(np.sum(g * g)) for g in grads.values()))
    if total > norm_limit > 0:
        scale = norm_limit / total
        return {key: g * scale for key, g in grads.items()}
    return grads


def _evaluate(net, matrices, labels, threshold=0.5):
    """二分类指标（正类 = 谣言）。样本为 0 时各项为 0。"""
    tp = fp = tn = fn = 0
    for matrix, label in zip(matrices, labels):
        _, p = net.forward(matrix)
        pred = 1 if p >= threshold else 0
        if pred and label:
            tp += 1
        elif pred and not label:
            fp += 1
        elif not pred and label:
            fn += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    total = tp + fp + tn + fn
    return {
        "accuracy": (tp + tn) / total if total else 0.0,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
    }


# ------------------------------------------------------------ 训练与工件

def train_tfidf_rnn(samples, hidden_size=DEFAULT_HIDDEN_SIZE, epochs=DEFAULT_EPOCHS,
                    batch_size=DEFAULT_BATCH_SIZE,
                    learning_rate=DEFAULT_LEARNING_RATE,
                    max_features=DEFAULT_MAX_FEATURES,
                    holdout_fraction=DEFAULT_HOLDOUT_FRACTION,
                    seed=DEFAULT_SEED,
                    max_sequence_length=DEFAULT_MAX_SEQUENCE_LENGTH,
                    data_note="",
                    test_samples=None,
                    input_mode="sequence",
                    progress=None):
    """训练 TF-IDF + RNN 模型，返回工件 dict。

    samples: [(sequence_texts, label), ...]，label 1=谣言 0=真实。
    词表只从训练折拟合（留出折的未登录词被忽略）；分层留出仅用于报告，
    超参数取固定缺省值，不在留出集上调参。

    test_samples：外部提供的测试折（如按近重复分组划分的 Weibo16）。
    提供时不做内部留出，训练只用 samples，测试折指标记入 stats["test"]。
    input_mode：sequence（正文+评论序列，PDF 完整路线）或 content
    （仅正文，消融实验用；推理时忽略评论）。
    progress：可选回调 progress(epoch, avg_loss)，每个 epoch 结束调用，
    供 CLI 打印长训练的进度。
    """
    if input_mode not in INPUT_MODES:
        raise ValueError("input_mode 必须是 sequence 或 content")
    positives = [s for s in samples if s[1] == 1]
    negatives = [s for s in samples if s[1] == 0]
    if len(positives) < 2 or len(negatives) < 2:
        raise ValueError("训练样本不足：需要至少各 2 条人工判定为虚假与真实的消息")
    if any(not seq for seq, _ in samples):
        raise ValueError("样本序列不能为空")
    if test_samples is not None:
        holdout_fraction = 0.0

    rng = np.random.default_rng(seed)

    def _split(group):
        order = rng.permutation(len(group)).tolist()
        hold = 0
        if holdout_fraction > 0 and len(group) >= 2:
            hold = max(1, int(round(len(group) * holdout_fraction)))
            hold = min(hold, len(group) - 1)
        hold_idx = set(order[:hold])
        train = [group[i] for i in range(len(group)) if i not in hold_idx]
        holdout = [group[i] for i in range(len(group)) if i in hold_idx]
        return train, holdout

    train_pos, hold_pos = _split(positives)
    train_neg, hold_neg = _split(negatives)
    train = train_pos + train_neg
    holdout = hold_pos + hold_neg

    vectorizer = TfidfVectorizer(max_features)
    vectorizer.fit([text for seq, _ in train for text in seq])
    if not vectorizer.vocabulary:
        raise ValueError("训练文本没有可用词（分词后全为空）")

    x_train = [vectorizer.transform(seq)[:max_sequence_length] for seq, _ in train]
    y_train = [label for _, label in train]
    x_holdout = [vectorizer.transform(seq)[:max_sequence_length] for seq, _ in holdout]
    y_holdout = [label for _, label in holdout]

    net = RnnNetwork(len(vectorizer.vocabulary), hidden_size, seed)
    optimizer = AdamOptimizer(net.params(), learning_rate)
    pos_weight = len(train_neg) / max(len(train_pos), 1)

    final_loss = 0.0
    for epoch in range(epochs):
        order = rng.permutation(len(train))
        epoch_loss = 0.0
        for start in range(0, len(train), batch_size):
            batch_idx = order[start:start + batch_size]
            grads = _zero_grads(net)
            for i in batch_idx:
                hs, p = net.forward(x_train[i])
                sample_grads, loss = net.backward(x_train[i], hs, p, y_train[i], pos_weight)
                epoch_loss += loss
                for key in grads:
                    grads[key] += sample_grads[key]
            scale = 1.0 / len(batch_idx)
            grads = _clip_grads({k: g * scale for k, g in grads.items()}, GRADIENT_CLIP_NORM)
            optimizer.step([grads[k] for k in ("Wxh", "Whh", "bh", "Why", "by")])
        final_loss = epoch_loss / len(train)
        if progress is not None:
            progress(epoch + 1, final_loss)

    train_metrics = _evaluate(net, x_train, y_train)
    holdout_metrics = _evaluate(net, x_holdout, y_holdout) if holdout else None
    test_metrics = None
    if test_samples:
        x_test = [vectorizer.transform(seq)[:max_sequence_length]
                  for seq, _ in test_samples]
        y_test = [label for _, label in test_samples]
        test_metrics = _evaluate(net, x_test, y_test)

    return {
        "format": ARTIFACT_FORMAT,
        "version": ARTIFACT_VERSION,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "input_mode": input_mode,
        "max_sequence_length": max_sequence_length,
        "hidden_size": hidden_size,
        "vocabulary": [token for token, _ in
                       sorted(vectorizer.vocabulary.items(), key=lambda kv: kv[1])],
        "idf": [float(x) for x in vectorizer.idf],
        "weights": {
            "Wxh": net.Wxh.tolist(),
            "Whh": net.Whh.tolist(),
            "bh": net.bh.tolist(),
            "Why": net.Why.tolist(),
            "by": [float(net.by[0])],
        },
        "stats": {
            "samples": len(samples),
            "positives": len(positives),
            "negatives": len(negatives),
            "train_samples": len(train),
            "holdout_samples": len(holdout),
            "test_samples": len(test_samples) if test_samples else 0,
            "epochs": epochs,
            "final_train_loss": round(final_loss, 6),
            "pos_weight": round(pos_weight, 4),
            "train": train_metrics,
            "holdout": holdout_metrics,
            "test": test_metrics,
            "data_note": data_note or "未注明",
        },
    }


def save_artifact(artifact, path):
    """原子写入工件（临时文件 + os.replace）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(artifact, fh, ensure_ascii=False)
    os.replace(tmp, path)


class LoadedSequenceModel:
    """从工件恢复的推理模型：词表 + IDF + RNN 权重。"""

    def __init__(self, artifact):
        if not isinstance(artifact, dict):
            raise ValueError("工件不是 JSON 对象")
        if artifact.get("format") != ARTIFACT_FORMAT:
            raise ValueError("工件格式不正确")
        if artifact.get("version") != ARTIFACT_VERSION:
            raise ValueError("工件版本不支持")
        vocabulary = artifact.get("vocabulary")
        idf = artifact.get("idf")
        weights = artifact.get("weights")
        if not isinstance(vocabulary, list) or not vocabulary:
            raise ValueError("工件缺少词表")
        if not isinstance(idf, list) or len(idf) != len(vocabulary):
            raise ValueError("工件 IDF 与词表不一致")
        if not isinstance(weights, dict):
            raise ValueError("工件缺少权重")
        input_mode = artifact.get("input_mode", "sequence")
        if input_mode not in INPUT_MODES:
            raise ValueError("工件 input_mode 不支持：{}".format(input_mode))
        self.input_mode = input_mode
        self.vocabulary = {token: idx for idx, token in enumerate(vocabulary)}
        self.idf = np.array(idf, dtype=np.float64)
        wxh = np.array(weights["Wxh"], dtype=np.float64)
        if wxh.ndim != 2 or wxh.shape[0] != len(vocabulary):
            raise ValueError("工件权重与词表不一致")
        self.max_sequence_length = int(artifact.get(
            "max_sequence_length", DEFAULT_MAX_SEQUENCE_LENGTH))
        self.stats = artifact.get("stats") if isinstance(artifact.get("stats"), dict) else {}
        self.net = RnnNetwork(len(vocabulary), wxh.shape[1], seed=0)
        self.net.Wxh = wxh
        self.net.Whh = np.array(weights["Whh"], dtype=np.float64)
        self.net.bh = np.array(weights["bh"], dtype=np.float64)
        self.net.Why = np.array(weights["Why"], dtype=np.float64)
        self.net.by = np.array(weights["by"], dtype=np.float64)

    def vectorize(self, texts):
        """按工件词表把文本序列转为特征矩阵（PDF 步骤 1/3）。"""
        matrix = np.zeros((len(texts), len(self.vocabulary)), dtype=np.float64)
        for row, text in enumerate(texts):
            counts = {}
            for token in tokenize(text):
                idx = self.vocabulary.get(token)
                if idx is not None:
                    counts[idx] = counts.get(idx, 0) + 1
            for idx, count in counts.items():
                matrix[row, idx] = count * self.idf[idx]
            norm = math.sqrt(float(np.dot(matrix[row], matrix[row])))
            if norm > 0:
                matrix[row] /= norm
        return matrix

    def predict(self, texts):
        """特征矩阵输入 RNN（PDF 步骤 4），返回谣言概率 (0,1)。"""
        _, p = self.net.forward(self.vectorize(texts))
        return p


def load_artifact(path):
    raw = Path(path).read_bytes()
    # 模型训练版本：工件内容哈希 + 输入模式。重新训练（数据或超参
    # 不同）产生不同哈希；仅重新加载同一工件时保持不变。工件格式的
    # version 字段是结构版本，不作为训练版本。
    digest = hashlib.sha256(raw).hexdigest()[:16]
    artifact = json.loads(raw)
    model = LoadedSequenceModel(artifact)
    model.version_label = "tfidf-rnn:{}:{}".format(digest, model.input_mode)
    return model


# ------------------------------------------------------------ 模型接口

class TfidfRnnModel(CheckModel):
    """接入平台模型工厂的本地序列模型（PDF 原路线）。"""

    name = "tfidf_rnn"
    display_name = "本地序列模型（TF-IDF+RNN）"
    description = (
        "按 PDF 路线本地实现：TF-IDF 文本特征提取，评论按时间排序组成序列矩阵，"
        "输入预训练 RNN 输出风险评分；离线训练，不依赖外部模型服务。"
    )

    def __init__(self):
        self._lock = threading.Lock()
        self._model = None
        self._stamp = None
        self._error = ""

    def _load(self):
        """加载工件（按 mtime+size 缓存，重新训练后无需重启即可生效）。"""
        path = artifact_path()
        try:
            stat = path.stat()
        except OSError:
            with self._lock:
                self._model, self._stamp = None, None
                self._error = (
                    "未找到本地序列模型工件（{}）；请先运行 "
                    "scripts/train_tfidf_rnn.py 完成训练".format(path)
                )
            return None
        stamp = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            if self._model is not None and self._stamp == stamp:
                return self._model
        try:
            model = load_artifact(path)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            with self._lock:
                self._model, self._stamp = None, None
                self._error = "本地序列模型工件无法加载（{}），请重新训练".format(
                    type(exc).__name__)
            return None
        with self._lock:
            self._model, self._stamp, self._error = model, stamp, ""
        return model

    def detect(self):
        return self._load() is not None

    @property
    def uses_comments(self):
        """序列模式工件使用评论输入；正文消融（content）工件不使用。"""
        model = self._load()
        return bool(model) and model.input_mode == "sequence"

    @property
    def model_version(self):
        """实际加载工件的版本标识（内容哈希+输入模式）；不可用时为空。"""
        model = self._load()
        return getattr(model, "version_label", "") if model is not None else ""

    def initialize(self):
        self._load()

    def unavailable_reason(self):
        self._load()
        return self._error or super().unavailable_reason()

    def check(self, message):
        """自由文本检测：无评论数据，序列只含正文一条。"""
        model = self._load()
        if model is None:
            raise CheckError(self._error or "本地序列模型不可用")
        return self._predict(model, [str(message or "")])

    def check_sequence(self, source_text, comments=None):
        """PDF 完整路线：正文 + 评论按时间排序组成序列后检测。

        正文消融（content 模式）工件忽略评论，退化为单文本输入——
        与训练口径一致，避免推理时使用了训练中不存在的输入。
        """
        model = self._load()
        if model is None:
            raise CheckError(self._error or "本地序列模型不可用")
        if model.input_mode == "content":
            texts = [str(source_text or "")]
        else:
            texts = build_sequence(source_text, comments, model.max_sequence_length)
        return self._predict(model, texts)

    @staticmethod
    def _predict(model, texts):
        p = model.predict(texts)
        stats = model.stats or {}
        parts = ["本地序列模型（TF-IDF+RNN）"]
        parts.append("输入序列 {} 条（正文 1＋评论 {}）".format(
            len(texts), max(0, len(texts) - 1)))
        if stats.get("samples"):
            parts.append("训练样本 {} 条（人工判定谣言 {} 条）".format(
                stats.get("samples"), stats.get("positives")))
        parts.append("数据说明：{}".format(stats.get("data_note") or "未注明"))
        parts.append("风险评分未经概率校准，仅供预警参考")
        return round(p * 100.0, 2), "；".join(parts) + "。"


MODEL_CLASS = TfidfRnnModel
