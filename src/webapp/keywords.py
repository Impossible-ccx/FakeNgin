"""从消息正文统计关键词；结果仅代表当前数据集中的词频。"""

from collections import Counter
from functools import lru_cache
from itertools import islice

import jieba

MAX_SAMPLE_MESSAGES = 500
MAX_KEYWORDS = 24
_STOP_WORDS = frozenset(
    "的 了 是 在 和 与 或 及 被 把 对 为 从 到 有 没有 一个 一些 这 那 这个 那个 "
    "这些 那些 我 我们 你 你们 他 她 它 他们 自己 什么 怎么 为什么 可以 不能 "
    "已经 还是 就是 不是 但是 因为 所以 如果 然后 现在 今天 昨天 明天 "
    "时候 进行 通过 关于 其中 可能 应该 需要 还有 表示 记者 消息 来源 转发 "
    "全文 网页 链接 视频 图片 文章 用户 发布 报道 真的 觉得 知道 看到 "
    "the and this that with from into for not are was were has have http https www com "
    "weibo sina html jpg png".split()
)


@lru_cache(maxsize=4)
def _count_keywords(texts):
    counts = Counter()
    for text in texts:
        for raw_token in jieba.cut_for_search(text.casefold()):
            token = raw_token.strip()
            if (2 <= len(token) <= 24 and token not in _STOP_WORDS
                    and token.isalnum() and not token.isdecimal()):
                counts[token] += 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:MAX_KEYWORDS]
    if not ranked:
        return ()
    smallest, largest = min(count for _, count in ranked), ranked[0][1]
    return tuple(
        (text, count, 3 if smallest == largest else 1 + round(4 * (count - smallest) / (largest - smallest)))
        for text, count in ranked
    )


def dataset_keywords(contents):
    """最多统计前 500 条消息，按正文缓存；返回独立结果副本及实际样本数。"""
    texts = tuple("" if content is None else str(content) for content in islice(contents, MAX_SAMPLE_MESSAGES))
    return [
        {"text": text, "count": count, "weight": weight}
        for text, count, weight in _count_keywords(texts)
    ], len(texts)
