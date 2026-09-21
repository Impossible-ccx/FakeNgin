"""模板模型：对所有输入固定输出 50% 虚假概率。"""

from .base import CheckModel


class TemplateModel(CheckModel):
    name = "template_model"
    display_name = "模板模型"
    description = "示例模型：对所有输入固定输出 50% 虚假概率。"
    def initialize(self):
        return 
    def check(self, message):
        return 50.0, "模板模型：固定输出 50%"


MODEL_CLASS = TemplateModel
