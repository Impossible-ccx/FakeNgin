"""谣言检测模型接口基类。

新增模型时，在 checkmodel 目录下新建 py 文件，定义继承 CheckModel 的类，
并通过模块级 MODEL_CLASS 暴露，最后在 checkmodel/__init__.py 的
MODEL_MODULES 中登记文件名即可。
"""


class CheckError(Exception):
    """检测过程失败（请求异常、结果无法解析等）。"""


class CheckModel:
    name = ""
    display_name = ""
    description = ""

    def initialize(self):
        """加载时初始化，默认无需处理。"""
        pass

    def detect(self):
        """检查模型在当前环境是否可用，默认恒可用。"""
        return True

    def check(self, message) -> tuple[float, str]:
        """检测消息，返回 (虚假概率 0-100, 额外信息字符串)。

        失败时抛出 CheckError。
        """
        raise NotImplementedError
