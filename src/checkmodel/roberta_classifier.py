"""本地微调的中文 RoBERTa 谣言分类器模型。

权重由 src/train_rumor_model.py 训练产出到 models/rumor-roberta/。
torch/transformers 为可选依赖：缺失或权重不存在时 detect() 返回 False，
该模型会被工厂过滤；推理失败会抛出 CheckError。

训练数据约定：标签 1 = 谣言，标签 0 = 非谣言，softmax 的第 1 维即虚假概率。
"""

from pathlib import Path

from .base import CheckError, CheckModel

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = PROJECT_ROOT / "models" / "rumor-roberta"
MAX_LENGTH = 256


class RobertaRumorClassifier(CheckModel):
    name = "roberta_rumor"
    display_name = "中文 RoBERTa 谣言分类器"
    description = "本地微调的分类模型，毫秒级推理，适合对全部数据批量计算虚假概率。"

    def detect(self):
        if not (MODEL_DIR / "config.json").exists():
            return False
        try:
            import torch  # noqa: F401
            import transformers  # noqa: F401
        except ImportError:
            return False
        return True

    def initialize(self):
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        self._tokenizer = AutoTokenizer.from_pretrained(str(MODEL_DIR))
        self._model = AutoModelForSequenceClassification.from_pretrained(str(MODEL_DIR))
        self._model.eval()

    def check(self, message):
        import torch

        try:
            inputs = self._tokenizer(
                str(message), truncation=True, max_length=MAX_LENGTH,
                return_tensors="pt",
            )
            with torch.no_grad():
                logits = self._model(**inputs).logits
            probabilities = torch.softmax(logits, dim=-1)[0]
            rumor_probability = float(probabilities[1])
        except CheckError:
            raise
        except Exception:
            raise CheckError("分类器推理失败")

        reason = "分类器输出：谣言 {:.0%} / 非谣言 {:.0%}".format(
            rumor_probability, 1 - rumor_probability)
        return rumor_probability * 100, reason


MODEL_CLASS = RobertaRumorClassifier
