"""本地序列模型（TF-IDF+RNN）与评论导入测试。

不访问真实模型服务；训练在内存中完成，工件写入临时目录。
"""

import math
from unittest.mock import patch

import numpy as np
import pytest

import checkmodel
from checkmodel import tfidf_rnn
from checkmodel.base import CheckError, CheckModel
from webapp import detection, newsdata


# ------------------------------------------------------------- 样例与夹具

def _rumor_sequence(index):
    return [
        "网传 不实 谣言 消息 {} 请勿轻信 网传 谣言".format(index),
        "假的 辟谣 不要 转发 谣言",
        "已经 辟谣 假的 网传",
    ]


def _normal_sequence(index):
    return [
        "官方 发布 公告 属实 {} 权威 说明 官方".format(index),
        "属实 官方 已 确认 真的",
        "官网 公告 属实",
    ]


def _make_samples(n_rumor=10, n_normal=10, with_comments=True):
    samples = []
    for i in range(n_rumor):
        seq = _rumor_sequence(i)
        samples.append((seq if with_comments else seq[:1], 1))
    for i in range(n_normal):
        seq = _normal_sequence(i)
        samples.append((seq if with_comments else seq[:1], 0))
    return samples


def _train_artifact(**kwargs):
    options = {
        "hidden_size": 12,
        "epochs": 120,
        "holdout_fraction": 0.2,
        "seed": 7,
        "data_note": "单元测试合成数据",
    }
    options.update(kwargs)
    return tfidf_rnn.train_tfidf_rnn(_make_samples(), **options)


@pytest.fixture(scope="module")
def trained_artifact_path(tmp_path_factory):
    """模块内共享：训练一次并保存工件。"""
    path = tmp_path_factory.mktemp("local_model") / "tfidf_rnn.json"
    tfidf_rnn.save_artifact(_train_artifact(), path)
    return path


# ------------------------------------------------------------- 文本预处理

def test_tokenizer_filters_symbols_and_lowercases():
    tokens = tfidf_rnn.tokenize("Hello, 世界！。。。 test123")
    assert "hello" in tokens
    assert "世界" in tokens
    assert "test123" in tokens
    assert "。。" not in tokens
    assert "," not in tokens
    assert all(t == t.lower() for t in tokens)


def test_tfidf_transform_normalizes_rows_and_ignores_unseen_tokens():
    vectorizer = tfidf_rnn.TfidfVectorizer()
    vectorizer.fit(["谣言 网传", "官方 公告", "谣言 官方"])
    matrix = vectorizer.transform(["谣言 谣言", "完全无关的未知词"])
    assert matrix.shape == (2, len(vectorizer.vocabulary))
    assert abs(np.linalg.norm(matrix[0]) - 1.0) < 1e-9
    assert matrix[0][vectorizer.vocabulary["网传"]] == 0.0
    assert not matrix[1].any()


def test_tfidf_idf_matches_smooth_formula():
    vectorizer = tfidf_rnn.TfidfVectorizer()
    vectorizer.fit(["谣言", "谣言 公告"])
    # idf = ln((1+N)/(1+df)) + 1；N=2，谣言 df=2，公告 df=1
    expected_common = math.log(3.0 / 3.0) + 1.0
    expected_rare = math.log(3.0 / 2.0) + 1.0
    assert abs(vectorizer.idf[vectorizer.vocabulary["谣言"]] - expected_common) < 1e-9
    assert abs(vectorizer.idf[vectorizer.vocabulary["公告"]] - expected_rare) < 1e-9


def test_build_sequence_orders_comments_by_time_and_truncates():
    comments = [
        {"id": 3, "content": "C", "publish_time": "2026-01-03 00:00:00"},
        {"id": 1, "content": "A", "publish_time": "2026-01-01 00:00:00"},
        {"id": 2, "content": "B", "publish_time": "2026-01-02 00:00:00"},
    ]
    assert tfidf_rnn.build_sequence("S", comments) == ["S", "A", "B", "C"]
    assert tfidf_rnn.build_sequence("S", comments, max_length=3) == ["S", "A", "B"]
    assert tfidf_rnn.build_sequence("S", None) == ["S"]
    # 缺失时间的评论排在最前（与 list_comments 排序口径一致）
    no_time = [{"id": 9, "content": "X", "publish_time": ""},
               {"id": 8, "content": "Y", "publish_time": "2026-01-01 00:00:00"}]
    assert tfidf_rnn.build_sequence("S", no_time) == ["S", "X", "Y"]


# ------------------------------------------------------------- RNN 与训练

def test_rnn_forward_deterministic_and_bounded():
    matrix = np.arange(15, dtype=float).reshape(3, 5) / 10.0
    net_a = tfidf_rnn.RnnNetwork(5, 8, seed=3)
    net_b = tfidf_rnn.RnnNetwork(5, 8, seed=3)
    net_c = tfidf_rnn.RnnNetwork(5, 8, seed=4)
    hs, p = net_a.forward(matrix)
    hs_b, p_b = net_b.forward(matrix)
    _, p_c = net_c.forward(matrix)
    assert hs.shape == (3, 8)
    assert p == p_b
    assert 0.0 < p < 1.0
    assert p != p_c


def test_training_converges_on_separable_data():
    artifact = _train_artifact()
    stats = artifact["stats"]
    assert stats["samples"] == 20
    assert stats["train"]["accuracy"] >= 0.95
    assert stats["holdout"]["accuracy"] >= 0.75
    assert 0.0 <= stats["final_train_loss"] < 0.5
    assert stats["data_note"] == "单元测试合成数据"


def test_training_with_explicit_test_split():
    """外部测试折模式：训练只用 samples，测试折指标单独报告，不做内部留出。"""
    train = _make_samples(8, 8)
    test = _make_samples(2, 2)
    artifact = tfidf_rnn.train_tfidf_rnn(
        train, hidden_size=12, epochs=80, holdout_fraction=0.2, seed=7,
        data_note="外部测试折", test_samples=test)
    stats = artifact["stats"]
    assert stats["train_samples"] == 16
    assert stats["holdout"] is None
    assert stats["test_samples"] == 4
    total = sum(stats["test"]["confusion"].values())
    assert total == 4
    assert stats["test"]["accuracy"] >= 0.75
    assert stats["data_note"] == "外部测试折"


def test_artifact_save_load_roundtrip(trained_artifact_path):
    loaded = tfidf_rnn.load_artifact(trained_artifact_path)
    # 训练时区分类别的词在词表中，未知词被忽略
    assert "网传" in loaded.vocabulary
    rumor_p = loaded.predict(_rumor_sequence(999))
    normal_p = loaded.predict(_normal_sequence(999))
    assert 0.0 < rumor_p < 1.0
    assert 0.0 < normal_p < 1.0
    assert rumor_p > normal_p
    # 两次加载预测一致（确定性）
    again = tfidf_rnn.load_artifact(trained_artifact_path)
    assert again.predict(_rumor_sequence(999)) == rumor_p


def test_load_artifact_rejects_broken_files(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        tfidf_rnn.load_artifact(path)
    path.write_text('{"format": "tfidf-rnn", "version": 99}', encoding="utf-8")
    with pytest.raises(ValueError):
        tfidf_rnn.load_artifact(path)


# ------------------------------------------------------------- 模型接口

def test_model_unavailable_without_artifact(monkeypatch, tmp_path):
    monkeypatch.setenv("FAKENGIN_LOCAL_MODEL_PATH", str(tmp_path / "absent.json"))
    model = tfidf_rnn.TfidfRnnModel()
    assert model.detect() is False
    assert "train_tfidf_rnn" in model.unavailable_reason()
    with pytest.raises(CheckError):
        model.check("网传 谣言")


def test_model_check_with_artifact(monkeypatch, trained_artifact_path):
    monkeypatch.setenv("FAKENGIN_LOCAL_MODEL_PATH", str(trained_artifact_path))
    model = tfidf_rnn.TfidfRnnModel()
    assert model.detect() is True
    probability, reason = model.check("网传 不实 谣言")
    assert 0.0 <= probability <= 100.0
    assert "TF-IDF" in reason
    assert "训练样本 20 条" in reason
    assert "单元测试合成数据" in reason
    assert "评论 0" in reason
    # 谣言式文本评分应高于官方通报式文本
    prob_rumor, _ = model.check("网传 谣言 请勿轻信")
    prob_normal, _ = model.check("官方 发布 公告 属实")
    assert prob_rumor > prob_normal


def test_check_sequence_uses_comment_history(monkeypatch, trained_artifact_path):
    monkeypatch.setenv("FAKENGIN_LOCAL_MODEL_PATH", str(trained_artifact_path))
    model = tfidf_rnn.TfidfRnnModel()
    source = "某地发生一起事件，具体情况有待进一步说明"
    rumor_comments = [
        {"id": 1, "content": _rumor_sequence(1)[1], "publish_time": "2026-01-01 10:00:00"},
        {"id": 2, "content": _rumor_sequence(1)[2], "publish_time": "2026-01-01 11:00:00"},
    ]
    normal_comments = [
        {"id": 1, "content": _normal_sequence(1)[1], "publish_time": "2026-01-01 10:00:00"},
        {"id": 2, "content": _normal_sequence(1)[2], "publish_time": "2026-01-01 11:00:00"},
    ]
    prob_plain, reason_plain = model.check(source)
    prob_rumor, reason_rumor = model.check_sequence(source, rumor_comments)
    prob_normal, reason_normal = model.check_sequence(source, normal_comments)
    assert "评论 0" in reason_plain
    assert "评论 2" in reason_rumor
    assert "评论 2" in reason_normal
    # 同一正文，评论内容决定序列走向：辟谣式评论抬分、确认式评论压分
    assert prob_rumor > prob_plain > prob_normal


def test_base_check_sequence_defaults_to_single_text():
    calls = []

    class SimpleModel(CheckModel):
        name = "simple"

        def check(self, message):
            calls.append(message)
            return 10.0, "ok"

    model = SimpleModel()
    assert model.check_sequence("文本", [{"content": "评论"}]) == (10.0, "ok")
    assert calls == ["文本"]


def test_detect_page_lists_local_model_after_reprobe(app):
    # 写工件到默认数据目录（会话级临时目录），结束后删除并重新探测还原
    default_path = tfidf_rnn.artifact_path()
    tfidf_rnn.save_artifact(_train_artifact(), default_path)
    try:
        checkmodel._loaded = False
        page = app.get("/detect")
        text = page.get_data(as_text=True)
        assert "本地序列模型（TF-IDF+RNN）" in text
    finally:
        default_path.unlink(missing_ok=True)
        checkmodel.reprobe()


# ------------------------------------------------------------- 队列集成

def _install_local_model():
    """把本地序列模型实例注入模型工厂（自动还原）。"""
    checkmodel._ensure_loaded()
    model = tfidf_rnn.TfidfRnnModel()
    return (
        patch.dict(checkmodel._instances, {model.name: model}, clear=False),
        patch.dict(checkmodel._available, {model.name: True}, clear=False),
    )


def test_worker_executes_local_model_with_comment_sequence(
        fresh_data_dir, monkeypatch, trained_artifact_path):
    monkeypatch.setenv("FAKENGIN_LOCAL_MODEL_PATH", str(trained_artifact_path))
    message_id = newsdata.append_message({
        "content": "官方 发布 公告 属实 队列测试",
        "publish_time": "2026-01-01 09:00:00",
    })
    newsdata.add_comment(message_id, "假的 辟谣 谣言 不要 转发",
                         publish_time="2026-01-01 10:00:00")
    newsdata.add_comment(message_id, "网传 消息 不可信",
                         publish_time="2026-01-01 11:00:00")

    patches = _install_local_model()
    with patches[0], patches[1]:
        created = detection.enqueue([message_id], model_id="tfidf_rnn")
        assert created == 1
        assert detection.run_pending() == 1

    runs = detection.runs_for_message(message_id)
    assert len(runs) == 1
    run = runs[0]
    assert run["status"] == "succeeded"
    assert run["model_id"] == "tfidf_rnn"
    assert 0.0 <= run["probability"] <= 100.0
    assert "评论 2" in run["reason"]


def test_verify_detect_route_selects_model(
        app, fresh_data_dir, monkeypatch, trained_artifact_path):
    monkeypatch.setenv("FAKENGIN_LOCAL_MODEL_PATH", str(trained_artifact_path))
    message_id = newsdata.append_message({"content": "路由选择模型的消息"})
    app.post("/login", data={"username": "admin", "password": "admin"})

    patches = _install_local_model()
    with patches[0], patches[1]:
        page = app.post("/verify/detect", data={
            "id": str(message_id), "model": "tfidf_rnn",
        }, follow_redirects=True)
        assert "已加入检测队列" in page.get_data(as_text=True)

    runs = detection.runs_for_message(message_id)
    assert len(runs) == 1
    assert runs[0]["model_id"] == "tfidf_rnn"
    # 选择不可用的模型时退回默认（空 model_id）
    page = app.post("/verify/detect", data={
        "id": str(message_id), "model": "no_such_model",
    }, follow_redirects=True)
    assert page.status_code == 200


# ------------------------------------------------------------- 评论导入

MSG_A = "消息甲：官方发布公告说明情况"
MSG_B = "消息乙：网传不实消息请核实"


def _write_csv(path, header, rows):
    import csv as csv_module
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv_module.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


def _seed_messages():
    newsdata.append_message({"content": MSG_A, "publish_time": "2026-01-01 08:00:00"})
    newsdata.append_message({"content": MSG_B, "publish_time": "2026-01-01 08:30:00"})
    return {row["content"]: row["id"] for row in newsdata.load_all()}


def _comments_file(tmp_path, rows):
    path = tmp_path / "comments.csv"
    _write_csv(path, newsdata.COMMENT_COLUMNS, rows)
    return path


def test_import_comments_csv_builds_reply_tree(fresh_data_dir, tmp_path):
    ids = _seed_messages()
    path = _comments_file(tmp_path, [
        ["R1", MSG_A, "", "第一层评论", "2026-01-01 09:00:00"],
        ["R2", MSG_A, "R1", "回复第一层", "2026-01-01 10:00:00"],
        ["R3", MSG_B, "", "另一消息的评论", "2026-01-01 09:30:00"],
    ])
    stats = newsdata.import_comments_csv(path)
    assert stats["errors"] == []
    assert stats["imported"] == 3

    comments_a = newsdata.list_comments(ids[MSG_A])
    assert [c["content"] for c in comments_a] == ["第一层评论", "回复第一层"]
    assert comments_a[1]["parent_id"] == comments_a[0]["id"]
    comments_b = newsdata.list_comments(ids[MSG_B])
    assert [c["content"] for c in comments_b] == ["另一消息的评论"]
    assert comments_b[0]["parent_id"] is None


def test_import_comments_csv_reimport_dedupes(fresh_data_dir, tmp_path):
    _seed_messages()
    rows = [
        ["R1", MSG_A, "", "第一层评论", "2026-01-01 09:00:00"],
        ["R2", MSG_A, "R1", "回复第一层", "2026-01-01 10:00:00"],
    ]
    path = _comments_file(tmp_path, rows)
    assert newsdata.import_comments_csv(path)["imported"] == 2
    stats = newsdata.import_comments_csv(path)
    assert stats["imported"] == 0
    assert stats["duplicates"] == 2
    assert stats["errors"] == []


def test_import_comments_csv_atomic_on_errors(fresh_data_dir, tmp_path):
    _seed_messages()
    path = _comments_file(tmp_path, [
        ["R1", MSG_A, "", "正常评论", "2026-01-01 09:00:00"],
        ["R2", MSG_A, "NOPE", "父引用不存在", "2026-01-01 10:00:00"],
    ])
    stats = newsdata.import_comments_csv(path)
    assert stats["imported"] == 0
    assert len(stats["errors"]) == 1
    assert "父评论" in stats["errors"][0]["error"]
    with newsdata.db.db_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM comments").fetchone()["n"] == 0


def test_import_comments_csv_rejects_unknown_message(fresh_data_dir, tmp_path):
    _seed_messages()
    path = _comments_file(tmp_path, [
        ["R1", "库里不存在的消息", "", "评论", "2026-01-01 09:00:00"],
    ])
    stats = newsdata.import_comments_csv(path)
    assert stats["imported"] == 0
    assert "所属消息不存在" in stats["errors"][0]["error"]


def test_import_comments_csv_rejects_cross_message_parent(fresh_data_dir, tmp_path):
    _seed_messages()
    path = _comments_file(tmp_path, [
        ["R1", MSG_A, "", "消息甲的评论", "2026-01-01 09:00:00"],
        ["R2", MSG_B, "R1", "挂在消息乙但父亲属于甲", "2026-01-01 10:00:00"],
    ])
    stats = newsdata.import_comments_csv(path)
    assert stats["imported"] == 0
    assert "不属于同一消息" in stats["errors"][0]["error"]


# ------------------------------------------------------------- 输入模式与版本

def test_content_mode_artifact_ignores_comments(tmp_path, monkeypatch):
    """input_mode=content 的工件（正文消融）推理时忽略评论。"""
    samples = [(["谣言样例甲"], 1), (["谣言样例乙"], 1),
               (["官方通报甲"], 0), (["官方通报乙"], 0)] * 3
    artifact = tfidf_rnn.train_tfidf_rnn(samples, hidden_size=4, epochs=3,
                                         input_mode="content")
    assert artifact["input_mode"] == "content"
    path = tmp_path / "content_model.json"
    tfidf_rnn.save_artifact(artifact, path)
    monkeypatch.setenv("FAKENGIN_LOCAL_MODEL_PATH", str(path))

    model = tfidf_rnn.TfidfRnnModel()
    assert model.detect()
    # 正文模型不使用评论：uses_comments 为 False，check_sequence 忽略评论
    assert model.uses_comments is False
    plain = model.check_sequence("谣言样例甲", [])
    with_comments = model.check_sequence(
        "谣言样例甲", [{"id": 1, "content": "任意评论", "publish_time": "2026-01-01"}])
    assert plain[0] == with_comments[0]
    # 版本标识包含工件哈希与输入模式
    assert model.model_version.startswith("tfidf-rnn:")
    assert ":content" in model.model_version


def test_sequence_mode_artifact_uses_comments_and_version(tmp_path, monkeypatch):
    samples = [(["谣言样例甲", "评论一"], 1), (["谣言样例乙", "评论二"], 1),
               (["官方通报甲", "评论三"], 0), (["官方通报乙", "评论四"], 0)] * 3
    artifact = tfidf_rnn.train_tfidf_rnn(samples, hidden_size=4, epochs=3)
    assert artifact["input_mode"] == "sequence"
    path = tmp_path / "seq_model.json"
    tfidf_rnn.save_artifact(artifact, path)
    monkeypatch.setenv("FAKENGIN_LOCAL_MODEL_PATH", str(path))

    model = tfidf_rnn.TfidfRnnModel()
    assert model.uses_comments is True
    version = model.model_version
    assert ":sequence" in version
    # 同一工件重复加载：版本标识稳定
    assert tfidf_rnn.TfidfRnnModel().model_version == version
    # 不同工件（不同内容）产生不同版本标识
    other = tmp_path / "other.json"
    tfidf_rnn.save_artifact(
        tfidf_rnn.train_tfidf_rnn(samples, hidden_size=4, epochs=4), other)
    monkeypatch.setenv("FAKENGIN_LOCAL_MODEL_PATH", str(other))
    assert tfidf_rnn.TfidfRnnModel().model_version != version


def test_old_artifact_without_input_mode_treated_as_sequence(
        tmp_path, monkeypatch):
    """旧格式工件（无 input_mode 字段）按 sequence 处理，行为兼容。"""
    samples = [(["谣言样例甲"], 1), (["谣言样例乙"], 1),
               (["官方通报甲"], 0), (["官方通报乙"], 0)] * 3
    artifact = tfidf_rnn.train_tfidf_rnn(samples, hidden_size=4, epochs=3)
    del artifact["input_mode"]
    path = tmp_path / "legacy.json"
    tfidf_rnn.save_artifact(artifact, path)
    monkeypatch.setenv("FAKENGIN_LOCAL_MODEL_PATH", str(path))

    model = tfidf_rnn.TfidfRnnModel()
    assert model.detect()
    assert model.uses_comments is True


def test_input_digest_matches_inference_sequence():
    """检测层指纹与模型推理使用同一序列规则（checkmodel.base）。"""
    from checkmodel.base import build_sequence as base_seq
    comments = [
        {"id": 2, "content": "乙", "publish_time": "2026-01-02"},
        {"id": 1, "content": "甲", "publish_time": "2026-01-01"},
    ]
    assert tfidf_rnn.build_sequence("正文", comments) == \
        base_seq("正文", comments) == ["正文", "甲", "乙"]
    # 截断规则一致（默认 32）
    many = [{"id": i, "content": "c{}".format(i), "publish_time": ""}
            for i in range(50)]
    assert len(base_seq("正文", many)) == 32
    assert tfidf_rnn.build_sequence("正文", many) == base_seq("正文", many)
