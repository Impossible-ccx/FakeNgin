"""检测队列与人工审核回归测试（使用假模型，不发真实推理请求）。"""

import time
from unittest.mock import patch

from webapp import detection, newsdata, reviews


class FakeModel:
    """可编程假模型：脚本化返回值或异常，模拟成功/失败/耗时。"""

    name = "fake_model"
    display_name = "假模型 (测试)"

    def __init__(self, behavior=None):
        self.behavior = behavior or {"default": (42.0, "测试理由")}
        self.calls = []

    def check(self, message):
        self.calls.append(message)
        if message in self.behavior:
            result = self.behavior[message]
        else:
            result = self.behavior.get("default", (0.0, "默认测试理由"))
        if isinstance(result, Exception):
            raise result
        time.sleep(0.01)
        return result


def _install_fake_model(behavior=None):
    """返回 (patches, fake)。patches 需以 with 启用；fake.name 可用作 model_id。"""
    import checkmodel

    # 先完成懒加载，避免 get_model 首次探测重建 _instances 把假模型冲掉
    checkmodel._ensure_loaded()
    fake = FakeModel(behavior)
    patches = (
        patch.dict(checkmodel._instances, {fake.name: fake}, clear=False),
        patch.dict(checkmodel._available, {fake.name: True}, clear=False),
    )
    return patches, fake


def _enqueue(message_id, fake):
    """入队并显式绑定假模型，避免测试触发真实模型请求。"""
    return detection.enqueue([message_id], model_id=fake.name)


def test_enqueue_dedupes_and_executes(fresh_data_dir):
    message_id = newsdata.append_message({"content": "待检测消息"})

    patches, fake = _install_fake_model()
    created = _enqueue(message_id, fake)
    assert created == 1
    # 重复点击不会创建重复任务
    assert _enqueue(message_id, fake) == 0

    with patches[0], patches[1]:
        status = detection.execute_run(
            detection.runs_for_message(message_id)[0]["id"])
    assert status == "succeeded"

    run = detection.runs_for_message(message_id)[0]
    assert run["status"] == "succeeded"
    assert run["probability"] == 42.0
    assert run["reason"] == "测试理由"
    assert run["content_digest"] == detection.content_digest("待检测消息")
    assert run["duration_ms"] is not None


def test_model_failure_recorded_and_retryable(fresh_data_dir):
    message_id = newsdata.append_message({"content": "会失败的消息"})
    patches, fake = _install_fake_model({"会失败的消息": RuntimeError("模型服务超时")})
    _enqueue(message_id, fake)
    with patches[0], patches[1]:
        status = detection.execute_run(
            detection.runs_for_message(message_id)[0]["id"])
    assert status == "failed"
    failed_run = detection.runs_for_message(message_id)[0]
    assert "模型服务超时" in failed_run["error"]

    # 重试成功：新建任务、历史保留
    patches, fake = _install_fake_model()
    with patches[0], patches[1]:
        detection.retry_run(failed_run["id"])
        detection.run_pending()
    runs = detection.runs_for_message(message_id)
    assert len(runs) == 2
    assert runs[1]["status"] == "failed"
    assert runs[0]["status"] == "succeeded"

    # 对成功任务重试被拒绝
    import pytest
    with pytest.raises(ValueError):
        detection.retry_run(runs[0]["id"])


def test_no_model_marks_run_failed(fresh_data_dir):
    """没有可用模型时任务失败并给出可理解提示，不伪造成功。"""
    message_id = newsdata.append_message({"content": "无模型消息"})
    detection.enqueue([message_id])
    run_id = detection.runs_for_message(message_id)[0]["id"]

    import checkmodel
    with patch.dict(checkmodel._instances, {}, clear=True), \
            patch.object(checkmodel, "get_models", return_value=[]):
        status = detection.execute_run(run_id)

    assert status == "failed"
    run = detection.runs_for_message(message_id)[0]
    assert "没有可用模型" in run["error"]


def test_content_edit_makes_run_stale(fresh_data_dir):
    """正文修改后旧检测动态过期，历史不被改写。"""
    message_id = newsdata.append_message({"content": "原始正文"})
    patches, fake = _install_fake_model()
    _enqueue(message_id, fake)
    with patches[0], patches[1]:
        detection.run_pending()

    run = detection.runs_for_message(message_id)[0]
    assert not detection.is_stale(run, "原始正文")

    current = newsdata.get_message(message_id)
    newsdata.update_message(message_id, current["version"], {"content": "修改后的正文"})

    assert detection.is_stale(run, "修改后的正文")
    # 历史记录本身未被改写
    assert detection.runs_for_message(message_id)[0]["status"] == "succeeded"


def test_recovery_marks_running_as_interrupted(fresh_data_dir):
    message_id = newsdata.append_message({"content": "中断的消息"})
    patches, fake = _install_fake_model()
    _enqueue(message_id, fake)
    run_id = detection.runs_for_message(message_id)[0]["id"]
    with detection.db.db_conn() as conn:
        conn.execute("UPDATE detection_runs SET status = 'running' WHERE id = ?", (run_id,))

    interrupted = detection.recover_interrupted()
    assert interrupted == 1
    assert detection.runs_for_message(message_id)[0]["status"] == "interrupted"


def test_review_history_independent_and_linked(fresh_data_dir):
    """审核历史独立保存、关联检测任务、审核人明确；模型结果不被覆盖。"""
    message_id = newsdata.append_message({"content": "需人工审核"})
    patches, fake = _install_fake_model({"需人工审核": (85.0, "高度可疑")})
    _enqueue(message_id, fake)
    with patches[0], patches[1]:
        detection.run_pending()
    run_id = detection.runs_for_message(message_id)[0]["id"]

    review_id = reviews.add_review(
        message_id, reviewer="alice", conclusion="虚假",
        evidence="官方已辟谣", note="第一次审核", detection_run_id=run_id)

    history = reviews.reviews_for_message(message_id)
    assert len(history) == 1
    assert history[0]["reviewer"] == "alice"
    assert history[0]["conclusion"] == "虚假"
    assert history[0]["detection_run_id"] == run_id

    # 第二次审核结论不同 → 追加历史，不覆盖第一次
    reviews.add_review(message_id, reviewer="bob", conclusion="证据不足")
    history = reviews.reviews_for_message(message_id)
    assert len(history) == 2
    assert history[0]["conclusion"] == "证据不足"
    assert history[1]["conclusion"] == "虚假"
    # 消息当前性质跟随最新审核
    assert newsdata.get_message(message_id)["nature"] == "证据不足"

    # 非法输入被拒绝
    import pytest
    with pytest.raises(ValueError):
        reviews.add_review(message_id, reviewer="", conclusion="虚假")
    with pytest.raises(ValueError):
        reviews.add_review(message_id, reviewer="alice", conclusion="随便写的")
    with pytest.raises(ValueError):
        reviews.add_review(message_id, reviewer="alice", conclusion="虚假",
                           detection_run_id=99999)


def test_worker_processes_queue_automatically(fresh_data_dir):
    message_id = newsdata.append_message({"content": "后台执行"})
    patches, fake = _install_fake_model()
    _enqueue(message_id, fake)
    with patches[0], patches[1]:
        deadline = time.time() + 10
        while time.time() < deadline:
            if detection.runs_for_message(message_id)[0]["status"] != "pending":
                break
            detection.run_pending()
            time.sleep(0.05)
    assert detection.runs_for_message(message_id)[0]["status"] == "succeeded"


# ------------------------------------------------------------- 输入指纹扩展

class FakeSequenceModel(FakeModel):
    """使用评论序列的假模型（uses_comments=True，PDF 序列路线口径）。"""

    name = "fake_sequence_model"
    display_name = "假序列模型 (测试)"
    uses_comments = True
    model_version = "fake-seq-v1"

    def __init__(self, behavior=None):
        super().__init__(behavior)
        self.sequence_calls = []

    def check_sequence(self, source_text, comments=None):
        self.sequence_calls.append((source_text, list(comments or [])))
        return self.check(source_text)


def _install_fake_sequence_model(behavior=None):
    import checkmodel

    checkmodel._ensure_loaded()
    fake = FakeSequenceModel(behavior)
    patches = (
        patch.dict(checkmodel._instances, {fake.name: fake}, clear=False),
        patch.dict(checkmodel._available, {fake.name: True}, clear=False),
    )
    return patches, fake


def _latest_run(message_id):
    return detection.runs_for_message(message_id)[0]


def test_sequence_run_records_input_kind_and_model_version(fresh_data_dir):
    message_id = newsdata.append_message({"content": "序列消息正文"})
    newsdata.add_comment(message_id, "第一条评论", publish_time="2026-09-01 10:00:00")

    patches, fake = _install_fake_sequence_model()
    detection.enqueue([message_id], model_id=fake.name)
    with patches[0], patches[1]:
        assert detection.execute_run(
            detection.runs_for_message(message_id)[0]["id"]) == "succeeded"

    run = _latest_run(message_id)
    assert run["input_kind"] == "sequence"
    assert run["model_version"] == "fake-seq-v1"
    # 模型确实收到了评论（序列口径）
    assert len(fake.sequence_calls[0][1]) == 1
    assert not detection.is_stale(
        run, "序列消息正文",
        newsdata.list_comments(message_id))


def test_comment_changes_invalidate_sequence_runs(fresh_data_dir):
    """评论的新增、修改、时间调整与删除都使序列模型旧结果过期。"""
    message_id = newsdata.append_message({"content": "序列消息正文"})
    first_cid = newsdata.add_comment(
        message_id, "第一条评论", publish_time="2026-09-01 10:00:00")
    newsdata.add_comment(message_id, "第二条评论",
                            publish_time="2026-09-01 11:00:00")

    patches, fake = _install_fake_sequence_model()
    detection.enqueue([message_id], model_id=fake.name)
    with patches[0], patches[1]:
        detection.execute_run(detection.runs_for_message(message_id)[0]["id"])
    run = _latest_run(message_id)

    def _stale():
        return detection.is_stale(run, "序列消息正文",
                                  newsdata.list_comments(message_id))

    assert not _stale()

    # 新增评论
    newsdata.add_comment(message_id, "第三条评论",
                            publish_time="2026-09-01 12:00:00")
    assert _stale()

    # 重新检测后恢复新鲜；修改评论内容再次过期
    detection.enqueue([message_id], model_id=fake.name)
    with patches[0], patches[1]:
        detection.execute_run(detection.runs_for_message(message_id)[0]["id"])
    run = _latest_run(message_id)
    assert not _stale()
    from webapp import db as webdb
    with webdb.db_conn() as conn:
        conn.execute("UPDATE comments SET content = ? WHERE id = ?",
                     ("被修改的评论", first_cid))
    assert _stale()

    # 发布时间调整（顺序变化）同样过期
    detection.enqueue([message_id], model_id=fake.name)
    with patches[0], patches[1]:
        detection.execute_run(detection.runs_for_message(message_id)[0]["id"])
    run = _latest_run(message_id)
    assert not _stale()
    with webdb.db_conn() as conn:
        conn.execute("UPDATE comments SET publish_time = ? WHERE id = ?",
                     ("2026-09-05 09:00:00", first_cid))
    assert _stale()

    # 删除全部评论后仍判定过期（与推理时输入不同）
    detection.enqueue([message_id], model_id=fake.name)
    with patches[0], patches[1]:
        detection.execute_run(detection.runs_for_message(message_id)[0]["id"])
    run = _latest_run(message_id)
    with webdb.db_conn() as conn:
        conn.execute("DELETE FROM comments WHERE message_id = ?", (message_id,))
    assert _stale()


def test_content_runs_not_staled_by_comment_changes(fresh_data_dir):
    """仅正文模型的结果不因评论变化而过期；正文变化仍然过期。"""
    message_id = newsdata.append_message({"content": "正文消息"})
    newsdata.add_comment(message_id, "一条评论",
                            publish_time="2026-09-01 10:00:00")

    patches, fake = _install_fake_model()
    detection.enqueue([message_id], model_id=fake.name)
    with patches[0], patches[1]:
        detection.execute_run(detection.runs_for_message(message_id)[0]["id"])
    run = _latest_run(message_id)
    assert run["input_kind"] == "content"

    newsdata.add_comment(message_id, "新评论",
                            publish_time="2026-09-02 10:00:00")
    assert not detection.is_stale(run, "正文消息",
                                  newsdata.list_comments(message_id))

    current = newsdata.get_message(message_id)
    newsdata.update_message(message_id, current["version"],
                            {"content": "正文消息（改）"})
    assert detection.is_stale(run, "正文消息（改）",
                              newsdata.list_comments(message_id))


def test_attach_latest_runs_marks_sequence_stale_in_lists(fresh_data_dir):
    """列表口径（列表/搜索/复核页共用）能识别评论导致的过期。"""
    message_id = newsdata.append_message({"content": "列表序列消息"})
    newsdata.add_comment(message_id, "评论甲",
                            publish_time="2026-09-01 10:00:00")
    patches, fake = _install_fake_sequence_model()
    detection.enqueue([message_id], model_id=fake.name)
    with patches[0], patches[1]:
        detection.execute_run(detection.runs_for_message(message_id)[0]["id"])

    rows = [newsdata.get_message(message_id)]
    detection.attach_latest_runs(rows)
    assert rows[0]["run_stale"] is False

    newsdata.add_comment(message_id, "评论乙",
                            publish_time="2026-09-01 12:00:00")
    rows = [newsdata.get_message(message_id)]
    detection.attach_latest_runs(rows)
    assert rows[0]["run_stale"] is True
