"""模型状态提示与重新探测机制测试（不访问真实模型服务）。"""

import os
from unittest.mock import patch

import checkmodel

API_ENV = {
    "MODEL_API_BASE_URL": "http://model.test:3333/v1",
    "MODEL_API_KEY": "test-secret",
    "MODEL_API_MODEL": "test-model",
    "MODEL_API_TIMEOUT": "12",
}


def test_unavailable_models_report_reasons(app):
    env = {k: v for k, v in os.environ.items() if not k.startswith("MODEL_API_")}
    with patch.dict(os.environ, env, clear=True):
        with patch.object(checkmodel, "_loaded", False):
            status = checkmodel.get_model_status()
            assert len(status) == len(checkmodel.MODEL_MODULES)
            for entry in status:
                assert entry["available"] is False
                assert entry["reason"], "不可用模型必须给出原因：{}".format(entry)
            assert checkmodel.get_models() == []


def test_reprobe_finds_model_after_config_available(app):
    env = {k: v for k, v in os.environ.items() if not k.startswith("MODEL_API_")}
    with patch.dict(os.environ, env, clear=True):
        with patch.object(checkmodel, "_loaded", False):
            assert checkmodel.reprobe() == 0
            assert checkmodel.get_models() == []

            # 配置就绪后（模拟模型服务恢复），重新探测即可发现，无需重启
            with patch.dict(os.environ, API_ENV):
                assert checkmodel.reprobe() == 1
                models = checkmodel.get_models()
                assert [m["id"] for m in models] == ["compatible_api"]
                assert checkmodel.get_model("compatible_api") is not None


def test_detect_page_shows_status_when_no_model(app):
    env = {k: v for k, v in os.environ.items() if not k.startswith("MODEL_API_")}
    with patch.dict(os.environ, env, clear=True):
        with patch.object(checkmodel, "_loaded", False):
            page = app.get("/detect")
            text = page.get_data(as_text=True)
            assert page.status_code == 200
            assert "当前没有可用模型" in text
            assert "重新探测" in text
            # 不可用时禁止提交检测
            assert "disabled" in text


def test_detect_post_without_model_shows_clear_error(app):
    env = {k: v for k, v in os.environ.items() if not k.startswith("MODEL_API_")}
    with patch.dict(os.environ, env, clear=True):
        with patch.object(checkmodel, "_loaded", False):
            page = app.post("/detect", data={"message": "测试消息"})
            text = page.get_data(as_text=True)
            assert "当前没有可用模型" in text
            assert "detect-score" not in text  # 不产生检测结果


def test_reprobe_endpoint_redirects_with_flash(app):
    env = {k: v for k, v in os.environ.items() if not k.startswith("MODEL_API_")}
    with patch.dict(os.environ, env, clear=True):
        with patch.object(checkmodel, "_loaded", False):
            page = app.post("/detect/reprobe", follow_redirects=True)
            text = page.get_data(as_text=True)
            assert page.status_code == 200
            assert "重新探测完成" in text
