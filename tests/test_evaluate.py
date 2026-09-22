"""评测框架离线自测：数据校验、指标计算、端到端流程（假模型，不发真实请求）。

注意：本文件验证的是评测工具本身的正确性，不构成任何模型准确率评测结论。
"""

import importlib.util
import statistics
from pathlib import Path

from test_detection_queue import FakeModel

from webapp import newsdata


def _load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_model.py"
    spec = importlib.util.spec_from_file_location("evaluate_model", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_load_dataset_validation(tmp_path):
    module = _load_script()

    good = tmp_path / "good.csv"
    good.write_text(
        "content,label\n消息甲,fake\n消息乙,real\n",
        encoding="utf-8")
    rows = module.load_dataset(good)
    assert len(rows) == 2

    bad_label = tmp_path / "bad_label.csv"
    bad_label.write_text("content,label\n消息甲,可能是谣言\n", encoding="utf-8")
    try:
        module.load_dataset(bad_label)
        assert False, "非法标签应拒绝运行"
    except ValueError as exc:
        assert "fake/real" in str(exc)

    empty = tmp_path / "empty.csv"
    empty.write_text("content,label\n,fake\n", encoding="utf-8")
    try:
        module.load_dataset(empty)
        assert False, "空正文应拒绝"
    except ValueError:
        pass


def test_metrics_computation():
    module = _load_script()
    predictions = [
        {"label": "fake", "predicted": "fake", "probability": 90},
        {"label": "fake", "predicted": "real", "probability": 10},
        {"label": "real", "predicted": "fake", "probability": 80},
        {"label": "real", "predicted": "real", "probability": 20},
        {"label": "fake", "predicted": "error", "probability": None},
    ]
    latencies = [0.1] * 5
    metrics = module.compute_metrics(predictions, latencies, failures=[4])

    assert metrics["confusion_matrix"] == {"tp": 1, "fp": 1, "fn": 1, "tn": 1,
                                           "error": 1}
    assert metrics["precision_fake"] == 0.5
    assert metrics["recall_fake"] == 0.5
    assert metrics["f1_fake"] == 0.5
    assert metrics["failure_rate"] == 0.2
    assert metrics["latency_ms"]["mean"] == 100


def test_end_to_end_with_fake_model(tmp_path):
    """假模型跑通全流程：报告可生成、失败计入、阈值生效。"""
    module = _load_script()

    dataset = tmp_path / "dataset.csv"
    lines = ["content,label"]
    expected = []
    for i in range(6):
        content = "样本{}号内容".format(i)
        label = "fake" if i % 2 == 0 else "real"
        lines.append("{},{}".format(content, label))
        expected.append((content, label))
    dataset.write_text("\n".join(lines) + "\n", encoding="utf-8")

    rows = module.load_dataset(dataset)
    fake = FakeModel({"default": (80.0, "测试")})
    predictions, latencies, failures = module.run_model(fake, rows, threshold=50)

    assert len(predictions) == 6
    assert not failures
    for pred, (content, label) in zip(predictions, expected):
        assert pred["label"] == label
        assert pred["predicted"] == ("fake" if pred["probability"] >= 50 else "real")
    assert statistics.mean(latencies) > 0

    metrics = module.compute_metrics(predictions, latencies, failures)
    assert metrics["total"] == 6

    # 报告 JSON 可序列化
    import json
    json.dumps(metrics, ensure_ascii=False)


def test_framework_needs_real_dataset_disclaimer():
    """文档边界：脚本 docstring 必须声明未运行于真实评测集不构成结论。"""
    module = _load_script()
    assert "不构成" in module.__doc__
    assert "教师确认" in module.__doc__ or "尚待" in module.__doc__
