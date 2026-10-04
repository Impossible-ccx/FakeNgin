"""微调中文 RoBERTa 谣言分类器。

用法：
    python src/train_rumor_model.py

数据来源：database/newsdata/output/ 下的 csv（nature 列 True=谣言 / False=非谣言），
不涉及 newsdata 顶层的应用数据。
产出：models/rumor-roberta/，供 checkmodel/roberta_classifier.py 加载。

依赖：pip install torch transformers
首次运行会下载基座模型；国内网络默认走 hf-mirror.com 镜像，
可通过 HF_ENDPOINT 环境变量覆盖。
"""

import json
import os
import random
import time
from pathlib import Path

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

import pandas as pd  # noqa: E402
import torch  # noqa: E402
from torch.utils.data import DataLoader, Dataset  # noqa: E402
from transformers import (  # noqa: E402
    AutoModelForSequenceClassification,
    AutoTokenizer,
    get_linear_schedule_with_warmup,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "database" / "newsdata" / "output"
OUTPUT_DIR = PROJECT_ROOT / "models" / "rumor-roberta"

BASE_MODEL = "hfl/chinese-roberta-wwm-ext"
MAX_LENGTH = 128
BATCH_SIZE = 16
EPOCHS = 4
LEARNING_RATE = 2e-5
WEIGHT_DECAY = 0.01
VAL_RATIO = 0.1
SEED = 42
POSITIVE_LABEL = "True"  # nature 为 True 表示谣言


def load_rumor_dataset():
    frames = []
    for path in sorted(DATA_DIR.glob("*.csv")):
        df = pd.read_csv(path, dtype=str).fillna("")
        if "nature" not in df.columns or "content" not in df.columns:
            continue
        df = df[df["nature"].isin(["True", "False"])]
        frames.append(df[["content", "nature"]])
    if not frames:
        raise SystemExit("未找到带 True/False 标签的数据，请检查 {}".format(DATA_DIR))

    data = pd.concat(frames, ignore_index=True)
    data = data.drop_duplicates(subset="content").reset_index(drop=True)
    data["label"] = (data["nature"] == POSITIVE_LABEL).astype(int)
    return data


class RumorDataset(Dataset):
    def __init__(self, texts, labels, tokenizer):
        self.encodings = tokenizer(
            list(texts), truncation=True, max_length=MAX_LENGTH, padding=True
        )
        self.labels = list(labels)

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, index):
        item = {key: torch.tensor(value[index]) for key, value in self.encodings.items()}
        item["labels"] = torch.tensor(self.labels[index])
        return item


def evaluate(model, loader):
    model.eval()
    correct = 0
    total = 0
    with torch.no_grad():
        for batch in loader:
            logits = model(
                input_ids=batch["input_ids"],
                attention_mask=batch["attention_mask"],
            ).logits
            preds = logits.argmax(dim=-1)
            correct += int((preds == batch["labels"]).sum())
            total += len(batch["labels"])
    return correct / max(total, 1)


def main():
    random.seed(SEED)
    torch.manual_seed(SEED)

    data = load_rumor_dataset()
    rumor_count = int(data["label"].sum())
    print("样本 {} 条：谣言 {} / 非谣言 {}".format(
        len(data), rumor_count, len(data) - rumor_count))
    print("标签 1 = 谣言，标签 0 = 非谣言")

    shuffled = data.sample(frac=1, random_state=SEED).reset_index(drop=True)
    val_size = max(1, int(len(shuffled) * VAL_RATIO))
    val_data = shuffled.iloc[:val_size]
    train_data = shuffled.iloc[val_size:]

    print("加载基座模型 {} ...".format(BASE_MODEL))
    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL)
    model = AutoModelForSequenceClassification.from_pretrained(BASE_MODEL, num_labels=2)

    train_loader = DataLoader(
        RumorDataset(train_data["content"], train_data["label"], tokenizer),
        batch_size=BATCH_SIZE,
        shuffle=True,
    )
    val_loader = DataLoader(
        RumorDataset(val_data["content"], val_data["label"], tokenizer),
        batch_size=BATCH_SIZE,
    )

    optimizer = torch.optim.AdamW(
        model.parameters(), lr=LEARNING_RATE, weight_decay=WEIGHT_DECAY)
    total_steps = len(train_loader) * EPOCHS
    scheduler = get_linear_schedule_with_warmup(
        optimizer,
        num_warmup_steps=max(1, total_steps // 10),
        num_training_steps=total_steps,
    )

    best_accuracy = 0.0
    for epoch in range(1, EPOCHS + 1):
        model.train()
        running_loss = 0.0
        for step, batch in enumerate(train_loader, 1):
            optimizer.zero_grad()
            output = model(**batch)
            output.loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            scheduler.step()
            running_loss += float(output.loss.detach())
            if step % 20 == 0:
                print("  epoch {} step {}/{} loss {:.4f}".format(
                    epoch, step, len(train_loader), running_loss / step))

        accuracy = evaluate(model, val_loader)
        print("epoch {} 完成：验证准确率 {:.4f}".format(epoch, accuracy))
        if accuracy >= best_accuracy:
            best_accuracy = accuracy
            model.save_pretrained(OUTPUT_DIR)
            tokenizer.save_pretrained(OUTPUT_DIR)

    meta = {
        "base_model": BASE_MODEL,
        "samples": len(data),
        "val_accuracy": round(best_accuracy, 4),
        "label_meaning": {"0": "非谣言", "1": "谣言"},
        "trained_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (OUTPUT_DIR / "train_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print("完成：模型保存到 {}，验证准确率 {:.4f}".format(OUTPUT_DIR, best_accuracy))


if __name__ == "__main__":
    main()
