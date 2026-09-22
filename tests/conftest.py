"""测试公共设置：src 加入 sys.path，默认数据目录指向临时目录。

所有测试不读写真实 database/ 业务数据。
"""

import os
import re
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# 初始管理员口令固定（仅测试环境）；真实环境由随机口令文件替代
os.environ.setdefault("FAKENGIN_ADMIN_PASSWORD", "admin")
os.environ.setdefault("FAKENGIN_DATA_DIR", tempfile.mkdtemp(prefix="fakengin_test_"))

import pytest  # noqa: E402

from webapp import bm25, db  # noqa: E402

# 测试进程禁止真实模型请求：剥离本地 .env 带入的模型配置。
# 需要模型配置的用例（test_compatible_api）自行用 patch.dict 提供假值。
for _key in [k for k in os.environ if k.startswith("MODEL_API_")]:
    del os.environ[_key]

_CSRF_RE = re.compile(r'name="_csrf_token" value="([0-9a-f]+)"')


class CSRFClient:
    """自动获取并携带 CSRF 令牌的测试客户端。"""

    def __init__(self, client):
        self._client = client
        self._token = None

    def _ensure_token(self):
        if self._token is None:
            page = self._client.get("/login")
            match = _CSRF_RE.search(page.get_data(as_text=True))
            self._token = match.group(1) if match else ""
        return self._token

    def post(self, url, data=None, **kwargs):
        data = dict(data or {})
        data.setdefault("_csrf_token", self._ensure_token())
        return self._client.post(url, data=data, **kwargs)

    def get(self, url, **kwargs):
        return self._client.get(url, **kwargs)


@pytest.fixture()
def fresh_data_dir(tmp_path, monkeypatch):
    """把全部数据路径指向本次测试的临时目录并初始化空库。"""
    monkeypatch.setattr(db, "DATABASE_DIR", tmp_path)
    monkeypatch.setattr(db, "DATABASE_FILE", tmp_path / "fakengin.db")
    monkeypatch.setattr(bm25, "SEARCH_INDEX_DIR", tmp_path / "searchindex")
    db.init_db()
    return tmp_path


@pytest.fixture()
def app(fresh_data_dir):
    """返回测试客户端（背后 app 使用本次测试的临时数据目录）。"""
    from webapp import create_app

    return CSRFClient(create_app().test_client())
