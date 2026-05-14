"""
Phase 3 – Fine-tune MuRIL for intent classification.

Reads data/intent_training_data.jsonl (produced by generate_training_data.py)
and fine-tunes a classification head on top of MuRIL using HuggingFace Trainer.

Output:
    models/muril-intent/        ← final model directory
    models/muril-intent-runs/   ← TensorBoard logs

Usage:
    python scripts/finetune_intent.py
    python scripts/finetune_intent.py --epochs 5 --batch-size 16 --lr 2e-5
    python scripts/finetune_intent.py --data data/training_big.jsonl --output models/muril-intent-v2
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "data" / "intent_training_data.jsonl"
DEFAULT_OUTPUT = ROOT / "models" / "muril-intent"
DEFAULT_RUNS = ROOT / "models" / "muril-intent-runs"
BASE_MODEL = "google/muril-base-cased"


# ── Label mapping ──────────────────────────────────────────────────────────────

INTENTS = [
    "ADD_SALE",
    "ADD_PAYMENT",
    "VIEW_BALANCE",
    "ADD_EXPENSE",
    "SEND_REMINDER",
    "VIEW_TRANSACTIONS",
    "ADD_CUSTOMER",
    "CANCEL",
]
LABEL2ID = {lab: i for i, lab in enumerate(INTENTS)}
ID2LABEL = {i: lab for lab, i in LABEL2ID.items()}


# ── Dataset helper ─────────────────────────────────────────────────────────────

def _load_jsonl(path: Path) -> list[dict]:
    records = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _split(records: list[dict], val_ratio: float = 0.15, seed: int = 42):
    random.seed(seed)
    shuffled = records[:]
    random.shuffle(shuffled)
    cut = max(1, int(len(shuffled) * (1 - val_ratio)))
    return shuffled[:cut], shuffled[cut:]


def _build_hf_dataset(records: list[dict], tokenizer, max_length: int = 128):
    try:
        from datasets import Dataset
    except ImportError:
        sys.exit("datasets package not installed. Run: pip install datasets")

    texts = [r["text"] for r in records]
    labels = [LABEL2ID.get(r["label"], 0) for r in records]

    encodings = tokenizer(
        texts,
        truncation=True,
        padding="max_length",
        max_length=max_length,
        return_tensors="pt",
    )

    dataset = Dataset.from_dict({
        "input_ids": encodings["input_ids"].tolist(),
        "attention_mask": encodings["attention_mask"].tolist(),
        "labels": labels,
    })
    return dataset


# ── Metrics ───────────────────────────────────────────────────────────────────

def _compute_metrics(eval_pred):
    try:
        import numpy as np
        from sklearn.metrics import accuracy_score, f1_score
    except ImportError:
        sys.exit("scikit-learn not installed. Run: pip install scikit-learn")

    logits, labels = eval_pred
    preds = np.argmax(logits, axis=-1)
    return {
        "accuracy": accuracy_score(labels, preds),
        "f1_macro": f1_score(labels, preds, average="macro"),
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Fine-tune MuRIL intent classifier")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--base-model", default=BASE_MODEL)
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--lr", type=float, default=3e-5)
    parser.add_argument("--max-length", type=int, default=128)
    parser.add_argument("--val-ratio", type=float, default=0.15)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    if not args.data.exists():
        sys.exit(
            f"Training data not found at {args.data}.\n"
            "Run: python scripts/generate_training_data.py"
        )

    try:
        from transformers import (
            AutoTokenizer,
            AutoModelForSequenceClassification,
            TrainingArguments,
            Trainer,
        )
    except ImportError:
        sys.exit("transformers package not installed. Run: pip install transformers torch")

    print(f"Loading tokenizer: {args.base_model}")
    tokenizer = AutoTokenizer.from_pretrained(
        args.base_model, cache_dir=str(ROOT / ".muril_cache")
    )

    records = _load_jsonl(args.data)
    print(f"Loaded {len(records)} records from {args.data}")

    label_counts = Counter(r["label"] for r in records)
    print("Label distribution:", dict(label_counts))

    # Filter out records whose label is not in our INTENTS list
    records = [r for r in records if r["label"] in LABEL2ID]

    train_records, val_records = _split(records, args.val_ratio, args.seed)
    print(f"Train: {len(train_records)}, Val: {len(val_records)}")

    train_dataset = _build_hf_dataset(train_records, tokenizer, args.max_length)
    val_dataset = _build_hf_dataset(val_records, tokenizer, args.max_length)

    print(f"Loading model: {args.base_model}")
    model = AutoModelForSequenceClassification.from_pretrained(
        args.base_model,
        num_labels=len(INTENTS),
        id2label=ID2LABEL,
        label2id=LABEL2ID,
        cache_dir=str(ROOT / ".muril_cache"),
    )

    args.output.mkdir(parents=True, exist_ok=True)
    DEFAULT_RUNS.mkdir(parents=True, exist_ok=True)

    training_args = TrainingArguments(
        output_dir=str(args.output),
        logging_dir=str(DEFAULT_RUNS),
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=args.batch_size * 2,
        learning_rate=args.lr,
        warmup_ratio=0.1,
        weight_decay=0.01,
        evaluation_strategy="epoch",
        save_strategy="epoch",
        load_best_model_at_end=True,
        metric_for_best_model="f1_macro",
        greater_is_better=True,
        seed=args.seed,
        fp16=False,
        no_cuda=True,       # force CPU — avoids MPS out-of-memory on Mac
        use_mps_device=False,
        report_to=[],
        logging_steps=10,
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=val_dataset,
        compute_metrics=_compute_metrics,
    )

    print("\nStarting fine-tuning…")
    trainer.train()

    print("\nEvaluating on validation set…")
    metrics = trainer.evaluate()
    print(json.dumps(metrics, indent=2))

    print(f"\nSaving model to {args.output}")
    trainer.save_model(str(args.output))
    tokenizer.save_pretrained(str(args.output))

    # Save label map alongside model for easy reloading in muril_service.py
    label_map_path = args.output / "label_map.json"
    label_map_path.write_text(
        json.dumps({"id2label": ID2LABEL, "label2id": LABEL2ID}, indent=2)
    )

    print(f"\nDone. Model saved to {args.output}")
    print("To use in production set MURIL_MODEL_NAME to this path in .env")


if __name__ == "__main__":
    main()
