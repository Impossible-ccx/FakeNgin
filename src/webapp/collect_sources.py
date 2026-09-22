"""服务端受控的消息源 allowlist。

- 界面只能提交来源 ID，不存在任何“提交任意 URL”的入口；
- 真实来源要求 HTTPS、无需登录、公开接口；
- allow_private / https_only=False 仅用于本地测试的模拟服务；
- 新增来源 = 在本文件登记（先完成调研、许可与小规模隔离验证，
  记录见 docs/验收记录.md）。
"""

SOURCES = {
    # 主选来源（调研与小规模隔离验证记录见 docs/验收记录.md）：
    # robots.txt 对通用客户端放行、无需登录、大陆直连实测可达。
    "solidot": {
        "id": "solidot",
        "name": "Solidot（奇客资讯）",
        "url": "https://www.solidot.org/index.rss",
        "format": "rss",
        "https_only": True,
        "max_items": 20,
        "import_source": "RSS采集·Solidot",
        "note": "中文科技资讯官方 RSS 2.0。robots.txt 对通用客户端 Allow: /；"
                "无需登录；直连实测可达；正文为纯文本全文，约 20 条。"
                "科技新闻不等于谣言，仅作待检测消息素材。",
    },
    # 备选来源：全文更长、更新更快，正文为 HTML（入库前自动转纯文本）。
    "ithome": {
        "id": "ithome",
        "name": "IT之家",
        "url": "https://www.ithome.com/rss/",
        "format": "rss",
        "https_only": True,
        "max_items": 20,
        "import_source": "RSS采集·IT之家",
        "note": "软媒旗下科技媒体官方 RSS 2.0。robots.txt 放行 /rss/；"
                "无需登录；直连实测可达；正文为 HTML 全文，自动清洗为纯文本。",
    },
}


def get_source(source_id):
    """按 ID 取来源条目；未知 ID 抛 ValueError。"""
    entry = SOURCES.get(source_id)
    if entry is None:
        raise ValueError("未知消息来源：{}".format(source_id))
    return entry


def register_source(entry):
    """登记（或覆盖）一个来源条目。仅供测试与部署脚本使用。

    entry 必须含 id、name、url、format；可选 https_only（默认 True）、
    allow_private（默认 False）、max_items（默认 20）、note。
    """
    required = ("id", "name", "url", "format")
    missing = [key for key in required if not entry.get(key)]
    if missing:
        raise ValueError("来源条目缺少字段：{}".format("、".join(missing)))
    if entry["format"] not in ("rss", "atom", "json"):
        raise ValueError("来源格式仅支持 rss / atom / json")
    SOURCES[entry["id"]] = dict(entry)


def list_sources():
    """全部已登记来源（含状态说明），按 ID 排序。"""
    return [dict(entry) for _, entry in sorted(SOURCES.items())]
