"""
MuRIL NLP service — lazy-loads google/muril-base-cased on first use.

Provides:
  • Language detection   (hi-Deva / hi-Latn / en)
  • Intent classification (zero-shot via template cosine similarity)
  • Named entity extraction (PERSON / AMOUNT / PRODUCT / DATE / QUANTITY)
  • Sentence embeddings  for semantic customer name matching

All heavy operations run in a thread-pool executor so they never block the
async event loop.  The service degrades gracefully when transformers/torch are
not installed — callers always receive a valid (but empty) analysis dict.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
from typing import Any

try:
    import numpy as np
    _numpy_available = True
except ImportError:
    np = None  # type: ignore[assignment]
    _numpy_available = False

from app.core.config import settings

_logger = logging.getLogger(__name__)

# ── Intent seed sentences (Hinglish + Hindi) ──────────────────────────────────
# Used for zero-shot classification: query embedding is compared against the
# mean embedding of each class's seed sentences.

_INTENT_TEMPLATES: dict[str, list[str]] = {
    "ADD_SALE": [
        "Raju ko aata diya",
        "customer ko maal becha",
        "sale entry karo",
        "2kg chawal 40 rupay kilo diya",
        "Ramesh ne saman liya",
        "mal de diya customer ko",
    ],
    "ADD_PAYMENT": [
        "Raju ne 500 diya",
        "payment mili",
        "customer ne paisa diya",
        "payment received karo",
        "Ramesh ne paise de diye",
        "500 rupay mila",
    ],
    "VIEW_BALANCE": [
        "Raju ka kitna baaki hai",
        "hisaab dikha",
        "balance check karo",
        "kitna udhar hai",
        "pending amount batao",
        "Ramesh ka hisaab batao",
    ],
    "ADD_EXPENSE": [
        "rent 2000 diya",
        "bijli ka bill",
        "kharcha add karo",
        "dukan ka kharch likhna hai",
        "petrol 500 ka",
        "labour ko paisa diya",
    ],
    "SEND_REMINDER": [
        "Raju ko reminder bhejo",
        "payment reminder send karo",
        "message bhejo baaki ke liye",
        "WhatsApp bhejo reminder",
    ],
    "VIEW_TRANSACTIONS": [
        "aaj ki sabhi entries",
        "transactions list dikha",
        "sabhi records batao",
        "history dekho",
        "entries dikhao",
    ],
    "ADD_CUSTOMER": [
        "naya customer add karo",
        "new customer banao",
        "customer register karo",
        "naam add karo database mein",
    ],
    "CANCEL": [
        "rehne do",
        "cancel karo",
        "nahi karna",
        "chhod do",
        "mat karo",
        "band karo",
    ],
}

# ── Entity type anchor sentences (for embedding-based disambiguation) ─────────

_ENTITY_ANCHORS: dict[str, list[str]] = {
    "PERSON": ["person name", "customer name", "aadmi ka naam", "banda"],
    "AMOUNT": ["rupees money amount", "paisa kitna", "amount rupay"],
    "PRODUCT": ["product item goods", "maal saman cheez", "item name"],
    "DATE": ["date time today yesterday", "aaj kal tarikh"],
    "QUANTITY": ["quantity units kilograms litres", "kitna maal weight"],
}

# ── Regex patterns ────────────────────────────────────────────────────────────

_DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]+")
_LATIN_WORD_RE = re.compile(r"[a-zA-Z]+")
_HINGLISH_MARKERS = re.compile(
    r"\b(ko|ne|ka|ki|ke|hai|tha|diya|liya|baaki|udhar|kitna|karo|"
    r"mein|se|par|wala|wali|bhai|ji)\b",
    re.IGNORECASE,
)

# Amount: optional Rs/₹ prefix then digits
_AMOUNT_RE = re.compile(r"(?:Rs\.?\s*|₹\s*)?(\d+(?:\.\d+)?)", re.IGNORECASE)

# Capitalized word in Latin script (potential PERSON)
_CAP_WORD_RE = re.compile(r"\b([A-Z][a-z]{1,25})\b")

# Common Hinglish product vocabulary
_PRODUCT_RE = re.compile(
    r"\b(aata|atta|chawal|rice|daal|dal|tel|oil|sugar|cheeni|namak|salt|"
    r"cement|rod|sand|paint|brick|tile|kapda|cloth|soap|shampoo|biscuit|"
    r"namkeen|doodh|milk|paneer|ghee|maida|besan|suji|poha|chana|rajma|"
    r"sarso|moong|masala|mirchi|haldi|jeera|dhaniya|sabzi)\b",
    re.IGNORECASE,
)

# Quantity: number + unit
_QTY_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*"
    r"(kg|kilo|kilogram|gram|g|litre|ltr|liter|piece|pcs|dozen|meter|m|"
    r"box|bori|packet|nag|basta|quintal|sack)",
    re.IGNORECASE,
)

# Date/temporal markers (Hinglish + English)
_DATE_RE = re.compile(
    r"\b(aaj|kal|parso|somvar|mangalvar|budhvar|guruvar|shukravar|shanivaar|"
    r"ravivar|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"today|yesterday|tomorrow)\b",
    re.IGNORECASE,
)

# Words that are definitely NOT person names
_NOT_PERSON = re.compile(
    r"^(sale|payment|expense|balance|hisaab|baaki|udhar|rupay|rupees|"
    r"paisa|total|amount|entry|record|maal|saman|transaction|customer|"
    r"shop|dukan|business|account|ok|yes|no|haan|nahi|thoda|kitna|karo|"
    r"diya|liya|mila|hua|hai|tha)$",
    re.IGNORECASE,
)


# ═══════════════════════════════════════════════════════════════════════════════
# MurilService
# ═══════════════════════════════════════════════════════════════════════════════

class MurilService:
    """
    Singleton MuRIL NLP service.

    Usage:
        await muril_service.initialize()            # call once at startup
        analysis = await muril_service.analyze(text)
        scores   = await muril_service.compute_name_similarities(query, names)
    """

    def __init__(self) -> None:
        self._tokenizer: Any = None
        self._model: Any = None
        self._intent_embeddings: dict[str, np.ndarray] | None = None
        self._entity_type_embeddings: dict[str, np.ndarray] | None = None
        self._available = False
        self._initialised = False
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    def is_available(self) -> bool:
        return self._available

    async def initialize(self) -> None:
        """Load model + pre-compute template embeddings. Call once at startup."""
        if self._initialised:
            return
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._load_sync)

    async def analyze(
        self,
        text: str,
        raw_text: str | None = None,
        lang_hint: str | None = None,
    ) -> dict:
        """
        Full MuRIL analysis pipeline.

        Returns:
          detected_language  — BCP-47 string
          intent             — intent label string
          intent_confidence  — float in [0, 1]
          entities           — list of {type, value, score}
          normalized_text    — text that was analysed
        """
        source = raw_text or text
        detected_lang = lang_hint or self.detect_language(source)

        if self._available:
            intent_task = asyncio.create_task(self._classify_intent(text))
            ner_task = asyncio.create_task(self._run_ner(source))
            intent, intent_conf = await intent_task
            entities = await ner_task
        else:
            intent, intent_conf = "UNCLEAR", 0.0
            entities = self._extract_entities_regex(source)

        return {
            "detected_language": detected_lang,
            "intent": intent,
            "intent_confidence": round(intent_conf, 4),
            "entities": entities,
            "normalized_text": text,
        }

    async def compute_name_similarities(
        self,
        query: str,
        candidate_names: list[str],
    ) -> list[float]:
        """
        Cosine similarity between [query] and each candidate name.
        Falls back to 0.0 per candidate if MuRIL is unavailable.
        """
        if not self._available or not candidate_names:
            return [0.0] * len(candidate_names)

        all_texts = [query] + candidate_names
        embs = await self._get_embeddings_batch(all_texts)
        if embs is None:
            return [0.0] * len(candidate_names)

        query_emb = embs[0]
        return [float(np.dot(query_emb, embs[i + 1])) for i in range(len(candidate_names))]

    # ── Language detection (no model required) ────────────────────────────────

    def detect_language(self, text: str) -> str:
        """
        Script-analysis-based language detection.
        Returns BCP-47: "hi-Deva" | "hi-Latn" | "en"
        """
        has_devanagari = bool(_DEVANAGARI_RE.search(text))
        has_latin = bool(_LATIN_WORD_RE.search(text))

        if has_devanagari and not has_latin:
            return "hi-Deva"
        if has_devanagari:
            return "hi-Latn"  # Mixed script → Hinglish
        # Latin only — check for Hinglish vocabulary
        if _HINGLISH_MARKERS.search(text):
            return "hi-Latn"
        return "en"

    # ── Model loading ─────────────────────────────────────────────────────────

    def _load_sync(self) -> None:
        with self._lock:
            if self._initialised:
                return
            if not _numpy_available:
                _logger.warning("numpy not installed — MuRIL disabled.")
                self._initialised = True
                return

            try:
                from transformers import AutoModel, AutoTokenizer

                model_name = settings.muril_model_name
                _logger.info("Loading MuRIL model: %s …", model_name)

                self._tokenizer = AutoTokenizer.from_pretrained(
                    model_name, cache_dir=settings.muril_cache_dir
                )
                self._model = AutoModel.from_pretrained(
                    model_name, cache_dir=settings.muril_cache_dir
                )
                self._model.eval()

                # Pre-compute class embeddings (intent + entity types)
                self._intent_embeddings = self._compute_class_means(_INTENT_TEMPLATES)
                self._entity_type_embeddings = self._compute_class_means(_ENTITY_ANCHORS)

                self._available = True
                _logger.info("MuRIL loaded — intent classes: %d", len(self._intent_embeddings))

            except ImportError:
                _logger.warning(
                    "transformers / torch / numpy not installed — MuRIL disabled. "
                    "Run: pip install transformers torch sentencepiece numpy"
                )
            except Exception as exc:
                _logger.error("MuRIL load failed: %s", exc, exc_info=True)
            finally:
                self._initialised = True

    # ── Embedding helpers ─────────────────────────────────────────────────────

    def _embed_sync(self, texts: list[str]) -> np.ndarray:
        """
        Synchronous embedding computation.
        Returns L2-normalised (N, hidden_size) array.
        """
        import torch

        inputs = self._tokenizer(
            texts,
            return_tensors="pt",
            truncation=True,
            max_length=128,
            padding=True,
        )
        with torch.no_grad():
            outputs = self._model(**inputs)

        # Mean pooling over non-padding tokens
        attn_mask = inputs["attention_mask"]             # (N, L)
        token_embs = outputs.last_hidden_state           # (N, L, H)
        mask_exp = attn_mask.unsqueeze(-1).expand(token_embs.size()).float()
        summed = torch.sum(token_embs * mask_exp, dim=1)
        count = torch.clamp(mask_exp.sum(dim=1), min=1e-9)
        embs: np.ndarray = (summed / count).numpy()      # (N, H)

        # L2 normalise → cosine similarity = dot product
        norms = np.linalg.norm(embs, axis=1, keepdims=True)
        return embs / np.maximum(norms, 1e-9)

    async def _get_embeddings_batch(self, texts: list[str]) -> np.ndarray | None:
        if not self._available:
            return None
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, self._embed_sync, texts)
        except Exception as exc:
            _logger.error("MuRIL embedding error: %s", exc)
            return None

    def _compute_class_means(
        self, class_templates: dict[str, list[str]]
    ) -> dict[str, np.ndarray]:
        """Pre-compute mean embedding for each class from template sentences."""
        result: dict[str, np.ndarray] = {}
        for label, sentences in class_templates.items():
            embs = self._embed_sync(sentences)   # (N, H)
            mean_emb = embs.mean(axis=0)         # (H,)
            # Re-normalise the mean
            norm = np.linalg.norm(mean_emb)
            result[label] = mean_emb / max(norm, 1e-9)
        return result

    # ── Intent classification ─────────────────────────────────────────────────

    async def _classify_intent(self, text: str) -> tuple[str, float]:
        """Zero-shot intent classification via cosine similarity to class means."""
        if not self._available or not self._intent_embeddings:
            return "UNCLEAR", 0.0

        embs = await self._get_embeddings_batch([text])
        if embs is None:
            return "UNCLEAR", 0.0

        query_emb = embs[0]
        best_label, best_score = "UNCLEAR", 0.0

        for label, class_emb in self._intent_embeddings.items():
            score = float(np.dot(query_emb, class_emb))
            if score > best_score:
                best_score, best_label = score, label

        if best_score < settings.muril_intent_threshold:
            return "UNCLEAR", best_score

        return best_label, best_score

    # ── Named entity recognition ──────────────────────────────────────────────

    async def _run_ner(self, text: str) -> list[dict]:
        """
        Hybrid NER: regex extraction + optional MuRIL disambiguation.
        Returns list of {type, value, score} sorted by score descending.
        """
        entities = self._extract_entities_regex(text)

        if self._available and self._entity_type_embeddings:
            entities = await self._disambiguate_persons(text, entities)

        entities.sort(key=lambda e: e["score"], reverse=True)
        # Deduplicate by value (keep highest score)
        seen: set[str] = set()
        deduped: list[dict] = []
        for e in entities:
            key = e["value"].lower()
            if key not in seen:
                seen.add(key)
                deduped.append(e)

        return deduped[:10]

    def _extract_entities_regex(self, text: str) -> list[dict]:
        """Rule-based entity extraction. Always available, no model required."""
        entities: list[dict] = []
        seen: set[str] = set()

        # QUANTITY (must come before AMOUNT to avoid double-counting numbers)
        for m in _QTY_RE.finditer(text):
            val = m.group(0)
            key = val.lower()
            if key not in seen:
                entities.append({"type": "QUANTITY", "value": val, "score": 0.92})
                seen.add(key)
                seen.add(m.group(1))    # Mark the numeric part as claimed

        # AMOUNT
        for m in _AMOUNT_RE.finditer(text):
            val = m.group(1)
            if val in seen:
                continue
            try:
                n = float(val)
            except ValueError:
                continue
            # Skip years (1900-2100) and 10-digit phone numbers
            if 1900 < n < 2100 and len(val) == 4:
                continue
            if len(val) == 10 and n > 6000000000:
                continue
            if n > 0:
                entities.append({"type": "AMOUNT", "value": val, "score": 0.95})
                seen.add(val)

        # PRODUCT
        for m in _PRODUCT_RE.finditer(text):
            val = m.group(0)
            key = val.lower()
            if key not in seen:
                entities.append({"type": "PRODUCT", "value": val, "score": 0.90})
                seen.add(key)

        # DATE
        for m in _DATE_RE.finditer(text):
            val = m.group(0)
            key = val.lower()
            if key not in seen:
                entities.append({"type": "DATE", "value": val, "score": 0.90})
                seen.add(key)

        # PERSON — Capitalized Latin words not in known non-person vocab
        for m in _CAP_WORD_RE.finditer(text):
            val = m.group(1)
            key = val.lower()
            if key not in seen and not _NOT_PERSON.match(val):
                entities.append({"type": "PERSON", "value": val, "score": 0.80})
                seen.add(key)

        # PERSON — Devanagari words (likely names in this context)
        for m in _DEVANAGARI_RE.finditer(text):
            val = m.group(0)
            key = val
            if key not in seen and 2 <= len(val) <= 20:
                entities.append({"type": "PERSON", "value": val, "score": 0.75})
                seen.add(key)

        return entities

    async def _disambiguate_persons(
        self, full_text: str, entities: list[dict]
    ) -> list[dict]:
        """
        Use MuRIL embeddings to re-classify ambiguous PERSON entities.
        Checks whether a word is closer to PERSON or PRODUCT embedding.
        """
        ambiguous = [e for e in entities if e["type"] == "PERSON" and e["score"] < 0.85]
        certain = [e for e in entities if e not in ambiguous]

        if not ambiguous:
            return entities

        context = full_text[:80]
        phrases = [f"{e['value']} mentioned in: {context}" for e in ambiguous]

        embs = await self._get_embeddings_batch(phrases)
        if embs is None:
            return entities

        person_emb = self._entity_type_embeddings["PERSON"]
        product_emb = self._entity_type_embeddings["PRODUCT"]

        refined: list[dict] = list(certain)
        for i, entity in enumerate(ambiguous):
            q = embs[i]
            p_score = float(np.dot(q, person_emb))
            pr_score = float(np.dot(q, product_emb))

            if pr_score > p_score + 0.02:
                # Reclassify as PRODUCT
                refined.append({**entity, "type": "PRODUCT", "score": min(0.88, 0.60 + pr_score * 0.35)})
            else:
                refined.append({**entity, "score": min(0.88, 0.62 + p_score * 0.30)})

        return refined


# ── Module-level singleton ────────────────────────────────────────────────────

muril_service = MurilService()
