"""
Phase 3 – Synthetic Hinglish training data generator.

Uses the OpenAI API to expand each seed sentence in
data/intent_templates.json into N paraphrases (default 20).

Output: data/intent_training_data.jsonl
  {"text": "...", "label": "ADD_SALE", "lang": "hi-Latn"}

Usage:
    python scripts/generate_training_data.py --samples-per-intent 20
    python scripts/generate_training_data.py --samples-per-intent 50 --output data/training_big.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_FILE = ROOT / "data" / "intent_templates.json"
DEFAULT_OUTPUT = ROOT / "data" / "intent_training_data.jsonl"

_SYSTEM_PROMPT = """\
You are a native Hinglish speaker who uses a small business ledger app called Khatabook.
Generate diverse, realistic utterances a shopkeeper might type.
Mix Hindi written in Roman script (Hinglish) with English naturally.
Use common abbreviations: rs, k (kilo), kg, pcs, qty, amt, bal.
Vary sentence length, formality, spelling, and use of digits vs words.
Return ONLY a JSON array of strings, no explanation."""


def _build_user_prompt(intent: str, seeds: list[str], n: int) -> str:
    seed_block = "\n".join(f"- {s}" for s in seeds)
    return (
        f"Intent: {intent}\n"
        f"Seed examples:\n{seed_block}\n\n"
        f"Generate {n} new paraphrases for this intent. "
        f"Vary script (Devanagari allowed occasionally), amounts, names, and phrasing. "
        f"Return a JSON array of {n} strings."
    )


def _generate_paraphrases(
    client,
    intent: str,
    seeds: list[str],
    n: int,
    model: str = "gpt-4o-mini",
    retries: int = 3,
) -> list[str]:
    prompt = _build_user_prompt(intent, seeds, n)
    for attempt in range(retries):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.9,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content
            parsed = json.loads(raw)
            # GPT may return {"sentences": [...]} or just [...]
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
            for v in parsed.values():
                if isinstance(v, list):
                    return [str(x) for x in v]
            print(f"  [WARN] Unexpected JSON shape for {intent}: {list(parsed.keys())}")
            return []
        except Exception as exc:
            wait = 2 ** attempt
            print(f"  [RETRY {attempt+1}/{retries}] {exc} — waiting {wait}s")
            time.sleep(wait)
    return []


def _detect_lang(text: str) -> str:
    devanagari = sum(1 for c in text if "ऀ" <= c <= "ॿ")
    if devanagari > len(text) * 0.3:
        return "hi-Deva"
    if any(c.isalpha() and ord(c) < 128 for c in text):
        return "hi-Latn"
    return "en"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate MuRIL fine-tuning data")
    parser.add_argument("--samples-per-intent", type=int, default=20)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    random.seed(args.seed)

    try:
        from openai import OpenAI
    except ImportError:
        sys.exit("openai package not installed. Run: pip install openai")

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        sys.exit("OPENAI_API_KEY environment variable not set.")

    client = OpenAI(api_key=api_key)

    templates: dict[str, list[str]] = json.loads(TEMPLATES_FILE.read_text())

    args.output.parent.mkdir(parents=True, exist_ok=True)
    total_written = 0

    with args.output.open("w", encoding="utf-8") as fout:
        for intent, seeds in templates.items():
            print(f"Generating {args.samples_per_intent} samples for {intent}…")

            # Always include seeds themselves
            for seed in seeds:
                record = {"text": seed, "label": intent, "lang": _detect_lang(seed)}
                fout.write(json.dumps(record, ensure_ascii=False) + "\n")
                total_written += 1

            paraphrases = _generate_paraphrases(
                client, intent, seeds, args.samples_per_intent, args.model
            )
            for text in paraphrases:
                text = text.strip()
                if not text:
                    continue
                record = {"text": text, "label": intent, "lang": _detect_lang(text)}
                fout.write(json.dumps(record, ensure_ascii=False) + "\n")
                total_written += 1

            print(f"  → {len(paraphrases)} paraphrases added")

    print(f"\nDone. {total_written} records written to {args.output}")


if __name__ == "__main__":
    main()
