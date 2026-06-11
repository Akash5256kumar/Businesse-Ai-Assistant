from __future__ import annotations

import json
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import inventory_service

_logger = logging.getLogger(__name__)

# ── Product name normalization ─────────────────────────────────────────────────
#
# Two-layer lookup:
#   1. _PRODUCT_ALIASES  — exact phrase → canonical name
#      Handles Hindi names, regional names, brand names, and known spellings.
#   2. _WORD_SUBS        — word-by-word substitution for compound phrases
#      Converts "basmati chawal" → "basmati rice" when "chawal"→"rice" is in the table.
#      The result is then re-checked against _PRODUCT_ALIASES.
#
# After normalization the name is passed to inventory_service where _match_score
# handles any remaining character-level typos (e.g. "basmti rice" → "basmati rice").

_PRODUCT_ALIASES: dict[str, str] = {
    # ── Vegetables ────────────────────────────────────────────────────────────
    "tamato": "tomato", "tamatar": "tomato", "tamaatar": "tomato",
    "vindi": "bhindi", "bindi": "bhindi",
    "aaloo": "aloo", "alu": "aloo",
    "gobhi": "gobi", "gobhee": "gobi",
    "matar": "mutter", "matter": "mutter", "mattar": "mutter",
    "baigan": "baingan", "brinjal": "baingan",
    "pyaz": "onion", "pyaaz": "onion",
    "adrak": "ginger", "lahsun": "garlic",
    "dhania": "coriander", "dhaniya": "coriander",
    "mirchi": "chilli", "hari mirchi": "green chilli",
    "nimbu": "lemon", "nimbo": "lemon",

    # ── Generic rice / chawal ──────────────────────────────────────────────────
    "chawal": "rice", "chaawal": "rice", "chavel": "rice", "chawl": "rice",
    "chawal rice": "rice",

    # ── Basmati Rice ──────────────────────────────────────────────────────────
    "basmati": "basmati rice",
    "basmaati": "basmati rice", "basmti": "basmati rice",
    "basmati chawal": "basmati rice", "basmti chawal": "basmati rice",
    "long grain rice": "basmati rice",

    # ── Aged / Extra-aged Basmati ─────────────────────────────────────────────
    "aged basmati": "aged basmati rice",
    "extra aged basmati": "aged basmati rice",

    # ── Mini Basmati ──────────────────────────────────────────────────────────
    "mini basmati": "mini basmati rice",
    "mini basmti": "mini basmati rice",
    "mini basmati chawal": "mini basmati rice",

    # ── Rajbhog Rice ─────────────────────────────────────────────────────────
    "rajbhog": "rajbhog rice", "raj bhog": "rajbhog rice",
    "rajbhog chawal": "rajbhog rice", "raj bhog chawal": "rajbhog rice",

    # ── Dubar Rice ───────────────────────────────────────────────────────────
    "dubar": "dubar rice", "dubra": "dubar rice", "dubara": "dubar rice",
    "dubar chawal": "dubar rice", "dubra chawal": "dubar rice",

    # ── Rozana Rice ──────────────────────────────────────────────────────────
    "rozana": "rozana rice", "rosana": "rozana rice",
    "rozana chawal": "rozana rice",

    # ── Sona Masoori Rice ─────────────────────────────────────────────────────
    "sona masoori": "sona masoori rice", "sona masuri": "sona masoori rice",
    "sona masoor": "sona masoori rice", "sonamasoori": "sona masoori rice",
    "sonamasuri": "sona masoori rice", "sona masuri rice": "sona masoori rice",
    "sona masoori chawal": "sona masoori rice",
    "sona masuri chawal": "sona masoori rice",
    # Spoken/regional variants of Masoori (mansouri, mansuri, masuri)
    "mansouri": "sona masoori rice", "mansouri rice": "sona masoori rice",
    "mansouri chawal": "sona masoori rice",
    "mansuri": "sona masoori rice", "mansuri rice": "sona masoori rice",
    "mansoor rice": "sona masoori rice", "masuri rice": "sona masoori rice",

    # ── Kolam Rice ───────────────────────────────────────────────────────────
    "kolam": "kolam rice", "kolam chawal": "kolam rice",
    "lachkari kolam": "amma lachkari kolam",
    "lachkari kolam chawal": "amma lachkari kolam",
    "lachkari kolam rice": "amma lachkari kolam",

    # ── Jeera Rice ───────────────────────────────────────────────────────────
    "jeera rice": "jeera rice", "jeera chawal": "jeera rice",
    "jeerakasala": "jeerakasala rice", "jeera kasala": "jeerakasala rice",

    # ── GobindoBhog (Old / Broken / Premium) ─────────────────────────────────
    # No "gobindobhog rice" — inventory has 3 variants; fuzzy match shows all as AMBIGUOUS
    "gobindo bhog": "gobindobhog",
    "gobindobhog chawal": "gobindobhog",

    # ── HMT Rice ─────────────────────────────────────────────────────────────
    "hmt": "hmt rice", "hmt chawal": "hmt rice",

    # ── IR64 Rice ────────────────────────────────────────────────────────────
    "ir64": "ir64 rice", "ir 64": "ir64 rice", "ir64 chawal": "ir64 rice",

    # ── Swarna Rice ──────────────────────────────────────────────────────────
    "swarna": "swarna rice", "swarna chawal": "swarna rice",

    # ── Ponni Rice ───────────────────────────────────────────────────────────
    "ponni": "ponni rice", "ponni chawal": "ponni rice",

    # ── Matta Rice ───────────────────────────────────────────────────────────
    "matta": "matta rice", "matta chawal": "matta rice",
    "kerala matta": "matta rice",

    # ── Sharbati Rice ─────────────────────────────────────────────────────────
    "sharbati": "sharbati rice", "sharbati chawal": "sharbati rice",
    "shabati": "sharbati rice", "shabati rice": "sharbati rice",

    # ── Sugandha Rice ─────────────────────────────────────────────────────────
    "sugandha": "sugandha rice", "sugandha chawal": "sugandha rice",

    # ── Mogra Rice ───────────────────────────────────────────────────────────
    "mogra": "mogra rice", "mogra chawal": "mogra rice",

    # ── Tukda / Broken Rice ───────────────────────────────────────────────────
    "tukda": "tukda rice", "tukda chawal": "tukda rice",
    "broken rice": "tukda rice", "tuta chawal": "tukda rice",

    # ── Golden Sella / White Sella ────────────────────────────────────────────
    "golden sella": "golden sella rice", "golden sella chawal": "golden sella rice",
    "white sella": "white sella rice", "white sella chawal": "white sella rice",

    # ── Brown Rice ───────────────────────────────────────────────────────────
    "brown rice": "brown rice", "brown chawal": "brown rice",

    # ── Steam / Raw / Parboiled Rice ──────────────────────────────────────────
    "steam rice": "steam rice", "steam chawal": "steam rice",
    "steamed rice": "steam rice",
    "raw rice": "raw rice", "kaccha chawal": "raw rice",
    "parboiled rice": "parboiled rice", "usna chawal": "parboiled rice",
    "sela rice": "parboiled rice", "sela chawal": "parboiled rice",

    # ── Jasmine Rice ──────────────────────────────────────────────────────────
    "jasmine rice": "jasmine rice", "jasmine chawal": "jasmine rice",

    # ── Miniket / Minketa ─────────────────────────────────────────────────────
    # Inventory product is "Miniket" (no "rice" suffix) — target must match exactly
    "minket": "miniket", "minket rice": "miniket",
    "minket chawal": "miniket", "minketa": "miniket",
    "minketa rice": "miniket", "mini keta": "miniket",
    "miniket rice": "miniket", "miniket chawal": "miniket",

    # ── Mingat Rice (distinct variety, not a typo of minket) ──────────────────
    "mingat": "mingat rice", "mingat rice": "mingat rice",
    "minget": "mingat rice", "minget rice": "mingat rice",
    "mingat chawal": "mingat rice", "minget chawal": "mingat rice",

    # ── Trade Rice ────────────────────────────────────────────────────────────
    "trade rice": "trade rice", "trade chawal": "trade rice",
    "tred rice": "trade rice", "traid rice": "trade rice",

    # ── Wada Kolam Rice ───────────────────────────────────────────────────────
    "wada kolam": "wada kolam rice", "vada kolam": "wada kolam rice",
    "wada kolam rice": "wada kolam rice", "vada kolam rice": "wada kolam rice",
    "wada kolam chawal": "wada kolam rice", "vada kolam chawal": "wada kolam rice",
    "sriram wada kolam": "wada kolam rice",
    "sriram wada kolam rice": "wada kolam rice",

    # ── Kali Mooch (inventory name — NOT "kali moong rice") ──────────────────
    "kali moong": "kali mooch", "kaali moong": "kali mooch",
    "kali moong rice": "kali mooch", "kaali moong rice": "kali mooch",
    "kali moong chawal": "kali mooch", "kaali moong chawal": "kali mooch",
    "kali mooch rice": "kali mooch", "kaali mooch": "kali mooch",
    "black moong rice": "kali mooch", "black moong": "kali mooch",

    # ── Yellow Rice ───────────────────────────────────────────────────────────
    "yellow rice": "yellow rice", "yellow chawal": "yellow rice",
    "pila chawal": "yellow rice", "peela chawal": "yellow rice",

    # ── Premium / Short-grain / Long-grain ────────────────────────────────────
    "premium rice": "premium rice", "premium chawal": "premium rice",
    "short grain rice": "short grain rice",

    # ══════════════════════════════════════════════════════════════════════════
    # ── Brand / Product-specific aliases ─────────────────────────────────────
    # Maps user spellings, abbreviations, and regional names → exact inventory
    # product names (lowercased).  Fuzzy matching handles residual typos.
    # ══════════════════════════════════════════════════════════════════════════

    # ── 1060 Basmati Rice ─────────────────────────────────────────────────────
    "1060": "1060 basmati rice",
    "1060 basmati": "1060 basmati rice",
    "1060 chawal": "1060 basmati rice",

    # ── Ali Baba ──────────────────────────────────────────────────────────────
    "alibaba": "ali baba",
    "alibaba rice": "ali baba",
    "ali baba rice": "ali baba",
    "ali baba chawal": "ali baba",

    # ── Amma Sona Mansoori Rice ───────────────────────────────────────────────
    "amma sona masoori": "amma sona mansoori rice",
    "amma sona mansoori": "amma sona mansoori rice",
    "amma sona masuri": "amma sona mansoori rice",
    "amma mansoori": "amma sona mansoori rice",
    "amma masoori": "amma sona mansoori rice",
    "amma sona masori": "amma sona mansoori rice",

    # ── Amma Lachkari Kolam ───────────────────────────────────────────────────
    "amma lachkari": "amma lachkari kolam",
    "amma kolam": "amma lachkari kolam",

    # ── Banskathi Rice ────────────────────────────────────────────────────────
    "banskathi": "banskathi rice",
    "banskati": "banskathi rice",
    "banskathi chawal": "banskathi rice",
    "banskati rice": "banskathi rice",
    "banskhati": "banskathi rice",

    # ── Biryani King No.1 Jammuni ─────────────────────────────────────────────
    "biryani king jammuni": "biryani king no.1 jammuni",
    "biryani king no 1": "biryani king no.1 jammuni",
    "biryani king no1": "biryani king no.1 jammuni",
    "bk jammuni": "biryani king no.1 jammuni",
    "jammuni": "biryani king no.1 jammuni",
    "jammuni rice": "biryani king no.1 jammuni",
    "jamuni": "biryani king no.1 jammuni",
    "jamuni rice": "biryani king no.1 jammuni",

    # ── Biryani King Saffron ──────────────────────────────────────────────────
    "bk saffron": "biryani king saffron",
    "biryani king zaffran": "biryani king saffron",
    "biryani king safron": "biryani king saffron",
    "biryani king zafran": "biryani king saffron",

    # ── BLS Rajasthan Premium ─────────────────────────────────────────────────
    "bls": "bls rajasthan premium",
    "bls rajasthan": "bls rajasthan premium",
    "bls premium": "bls rajasthan premium",
    "bls rice": "bls rajasthan premium",

    # ── Bullet Rice ───────────────────────────────────────────────────────────
    "bullet": "bullet rice",
    "bullet chawal": "bullet rice",

    # ── Chakarpani Dark Pink ──────────────────────────────────────────────────
    "chakarpani": "chakarpani dark pink",
    "chakerpani": "chakarpani dark pink",
    "chakrapani": "chakarpani dark pink",
    "chakarpani rice": "chakarpani dark pink",

    # ── Dehraduni Dhoom ───────────────────────────────────────────────────────
    "dehraduni": "dehraduni dhoom",
    "dehra duni": "dehraduni dhoom",
    "dehradoon dhoom": "dehraduni dhoom",
    "dehradun dhoom": "dehraduni dhoom",
    "dehraduni chawal": "dehraduni dhoom",
    "dheraduni": "dehraduni dhoom",
    "dheraduni dhoom": "dehraduni dhoom",

    # ── Delhi Pasand series ───────────────────────────────────────────────────
    "dp light": "delhi pasand light",
    "dp easy": "delhi pasand easy",
    "dp super": "delhi pasand super",
    "dp regular": "delhi pasand regular",
    "dp fine": "delhi pasand fine",
    "dp daily": "delhi pasand daily",
    "dp tibar": "delhi pasand tibar",
    "dp tune": "delhi pasand tune",
    "dp aabha": "delhi pasand aabha",
    "dp first choice": "delhi pasand first choise",
    "dp first choise": "delhi pasand first choise",
    "dp fc": "delhi pasand first choise",
    "dp sona": "delhi pasand sona masoori",
    "dp sona masoori": "delhi pasand sona masoori",
    "dp sona mansoori": "delhi pasand sona masoori",
    "dilli pasand light": "delhi pasand light",
    "dilli pasand easy": "delhi pasand easy",
    "dilli pasand super": "delhi pasand super",
    "dilli pasand regular": "delhi pasand regular",
    "dilli pasand fine": "delhi pasand fine",
    "dilli pasand daily": "delhi pasand daily",
    "dilli pasand tibar": "delhi pasand tibar",
    "dilli pasand tune": "delhi pasand tune",
    "dilli pasand aabha": "delhi pasand aabha",
    "dilli pasand sona": "delhi pasand sona masoori",
    "delhi pasand first choice": "delhi pasand first choise",
    "delhi pasand sona mansoori": "delhi pasand sona masoori",

    # ── Divine ────────────────────────────────────────────────────────────────
    "divine rice": "divine",
    "divine chawal": "divine",

    # ── Dosa Rice ─────────────────────────────────────────────────────────────
    "dosa chawal": "dosa rice",
    "dosa": "dosa rice",

    # ── Double Chabi Golden Sella Basmati ─────────────────────────────────────
    "double chabi": "double chabi golden sella basmati",
    "double chabi golden sella": "double chabi golden sella basmati",
    "double chabi basmati": "double chabi golden sella basmati",
    "doble chabi": "double chabi golden sella basmati",
    "double chabi sella": "double chabi golden sella basmati",
    "double chabi chawal": "double chabi golden sella basmati",

    # ── Galaxy 1121 ───────────────────────────────────────────────────────────
    "galaxy": "galaxy 1121",
    "galaxy rice": "galaxy 1121",
    "galaxy basmati": "galaxy 1121",
    "galaxy 1121 rice": "galaxy 1121",
    "galaxy 1121 basmati": "galaxy 1121",

    # ── Gauri series ─────────────────────────────────────────────────────────
    "gauri naya": "gauri new",
    "gauri nai": "gauri new",
    "gauri nayi": "gauri new",
    "gauri purana": "gauri old",
    "gauri purani": "gauri old",
    "gauri sona": "gauri sona masoori",
    "gauri mansoori": "gauri sona masoori",
    "gauri masoori": "gauri sona masoori",
    "gauri sona mansoori": "gauri sona masoori",

    # ── GobindoBhog series ────────────────────────────────────────────────────
    "gobindobhog purana": "gobindobhog old",
    "gobindobhog purani": "gobindobhog old",
    "gobindo bhog purana": "gobindobhog old",
    "gobindo bhog old": "gobindobhog old",
    "gobindobhog tuta": "gobindobhog broken",
    "gobindo bhog tuta": "gobindobhog broken",
    "gobindobhog toota": "gobindobhog broken",
    "gobindo bhog broken": "gobindobhog broken",
    "gobindo bhog premium": "gobindobhog premium",

    # ── Gulmehak 1121 Creamy Sella ────────────────────────────────────────────
    "gulmehak": "gulmehak 1121 creamy sella",
    "gulmehaak": "gulmehak 1121 creamy sella",
    "gulmehak 1121": "gulmehak 1121 creamy sella",
    "gulmehak creamy": "gulmehak 1121 creamy sella",
    "gulmehak creamy sella": "gulmehak 1121 creamy sella",

    # ── Hare Krishna Punni Rice ───────────────────────────────────────────────
    "hare krishna": "hare krishna punni rice",
    "hari krishna": "hare krishna punni rice",
    "hari krishna rice": "hare krishna punni rice",
    "punni": "hare krishna punni rice",
    "punni rice": "hare krishna punni rice",

    # ── Hari Patti Natural ────────────────────────────────────────────────────
    "hari patti": "hari patti natural",
    "haripatti": "hari patti natural",
    "hari patti rice": "hari patti natural",
    "green patti": "hari patti natural",

    # ── Haryana No.1 Lajeez ───────────────────────────────────────────────────
    "haryana lajeez": "haryana no.1 lajeez",
    "haryana no 1 lajeez": "haryana no.1 lajeez",
    "haryana no1 lajeez": "haryana no.1 lajeez",
    "lajeez": "haryana no.1 lajeez",
    "lajeez rice": "haryana no.1 lajeez",

    # ── Idli Rice ─────────────────────────────────────────────────────────────
    "idli chawal": "idli rice",
    "idli": "idli rice",

    # ── Inaya series ──────────────────────────────────────────────────────────
    "inaya xxxl": "inaya xxxl golden",
    "inaya golden": "inaya xxxl golden",
    "inaya enjoy": "inaya enjoy rice",
    "inaya traditional": "inaya traditional basmati rice",
    "inaya basmati": "inaya traditional basmati rice",
    "inaya traditional basmati": "inaya traditional basmati rice",
    "inaya lotus": "inaya lotus rice",

    # ── India Gate series ─────────────────────────────────────────────────────
    "india gate sona": "india gate sona mansoori",
    "india gate mansoori": "india gate sona mansoori",
    "india gate masoori": "india gate sona mansoori",
    "india gate sona masoori": "india gate sona mansoori",
    "india gate masuri": "india gate sona mansoori",
    "ig kolam": "india gate kolam",
    "ig sona": "india gate sona mansoori",

    # ── Indrani Rice ──────────────────────────────────────────────────────────
    "indrani": "indrani rice",
    "indrani chawal": "indrani rice",

    # ── Jammu Rice ────────────────────────────────────────────────────────────
    "jammu": "jammu rice",
    "jammu chawal": "jammu rice",
    "jammu ka chawal": "jammu rice",

    # ── Khaajana Rice ─────────────────────────────────────────────────────────
    "khajana": "khaajana rice",
    "khazana": "khaajana rice",
    "khaajana": "khaajana rice",
    "khajana rice": "khaajana rice",
    "khazana rice": "khaajana rice",
    "khajana chawal": "khaajana rice",

    # ── Kitchen Champion series ───────────────────────────────────────────────
    "kc mogra": "kitchen champion mogra",
    "kc mini mogra": "kitchen champion mini mogra",
    "kitchen mogra": "kitchen champion mogra",
    "kitchen mini mogra": "kitchen champion mini mogra",
    "kitchen champion": "kitchen champion mogra",

    # ── Kolam Raw ─────────────────────────────────────────────────────────────
    "raw kolam": "kolam raw",
    "kolam kaccha": "kolam raw",
    "kaccha kolam": "kolam raw",

    # ── Kolaberi Rice ─────────────────────────────────────────────────────────
    "kolaberi": "kolaberi rice",
    "kolabri": "kolaberi rice",
    "kolaveri": "kolaberi rice",
    "kolaberi chawal": "kolaberi rice",

    # ── Lahori Gate Rozana ────────────────────────────────────────────────────
    "lahori gate": "lahori gate rozana",
    "lahori rozana": "lahori gate rozana",
    "lahori": "lahori gate rozana",
    "lahori chawal": "lahori gate rozana",

    # ── Bina Gold Miniket ─────────────────────────────────────────────────────
    "bina gold": "bina gold miniket",
    "bina gold miniket rice": "bina gold miniket",
    "bina miniket": "bina gold miniket",

    # ── Naira / Zeeba Golden Sella series ────────────────────────────────────
    "naira mongra": "naira/zeeba golden sella mongra",
    "naira golden mongra": "naira/zeeba golden sella mongra",
    "naira zeeba mongra": "naira/zeeba golden sella mongra",
    "naira/zeeba mongra": "naira/zeeba golden sella mongra",
    "zeeba mongra": "naira/zeeba golden sella mongra",
    "naira tibar": "naira/zeeba golden sella tibar",
    "naira golden tibar": "naira/zeeba golden sella tibar",
    "naira zeeba tibar": "naira/zeeba golden sella tibar",
    "naira/zeeba tibar": "naira/zeeba golden sella tibar",

    # ── Noor e Eshaan Diamond ─────────────────────────────────────────────────
    "noor eshaan": "noor e eshaan diamond",
    "noor e eshaan": "noor e eshaan diamond",
    "noor ishaan": "noor e eshaan diamond",
    "noor eshan": "noor e eshaan diamond",
    "noor diamond": "noor e eshaan diamond",

    # ── Parmal series ─────────────────────────────────────────────────────────
    "parmal 1": "parmal no. 1",
    "parmal no 1": "parmal no. 1",
    "parmal no1": "parmal no. 1",
    "parmal number 1": "parmal no. 1",
    "parmal 3": "parmal no. 3",
    "parmal no 3": "parmal no. 3",
    "parmal no3": "parmal no. 3",
    "parmal number 3": "parmal no. 3",

    # ── Patanjali series ──────────────────────────────────────────────────────
    "patanjali mogra": "patanjali shakti mogra rice",
    "patanjali shakti mogra": "patanjali shakti mogra rice",
    "patanjali xxl": "patanjali shakti xxl mini dubar rice",
    "patanjali xxl dubar": "patanjali shakti xxl mini dubar rice",
    "patanjali shakti xxl": "patanjali shakti xxl mini dubar rice",
    "patanjali dubar": "patanjali shakti xxl mini dubar rice",

    # ── Patliputra Farm Katarni Steam ─────────────────────────────────────────
    "katarni": "patliputra farm katarni steam",
    "katarni rice": "patliputra farm katarni steam",
    "katarni steam": "patliputra farm katarni steam",
    "patliputra": "patliputra farm katarni steam",
    "patliputra katarni": "patliputra farm katarni steam",
    "katarni chawal": "patliputra farm katarni steam",
    "patliputra farm": "patliputra farm katarni steam",

    # ── Pavizham series ───────────────────────────────────────────────────────
    "wadi matta": "pavizham wadi matta rice",
    "pavizham wadi": "pavizham wadi matta rice",
    "pavizham wadi matta": "pavizham wadi matta rice",
    "short matta": "pavizham short matta rice",
    "pavizham short": "pavizham short matta rice",
    "pavizham short matta": "pavizham short matta rice",
    "pavizham matta": "pavizham wadi matta rice",

    # ── Perfect Sela ──────────────────────────────────────────────────────────
    "perfect sella": "perfect sela",
    "perfect sela rice": "perfect sela",
    "perfect sella rice": "perfect sela",

    # ── Punjab Ratan ──────────────────────────────────────────────────────────
    "pb ratan": "punjab ratan",
    "punjab rattan": "punjab ratan",

    # ── Queen of Punjab ───────────────────────────────────────────────────────
    "queen punjab": "queen of punjab",
    "queen of punjab rice": "queen of punjab",

    # ── Rani Sahiba / Rani Sahiba Old ─────────────────────────────────────────
    "rani saheba": "rani sahiba",
    "rani sahiba naya": "rani sahiba",
    "rani saheba old": "rani sahiba old",
    "rani sahiba purana": "rani sahiba old",
    "rani saheba purana": "rani sahiba old",
    "rani sahiba purani": "rani sahiba old",

    # ── Red Rice ──────────────────────────────────────────────────────────────
    "red chawal": "red rice",
    "lal chawal": "red rice",
    "lal rice": "red rice",

    # ── Resort A1 Sella ───────────────────────────────────────────────────────
    "resort": "resort a1 sella",
    "resort sella": "resort a1 sella",
    "resort a1": "resort a1 sella",
    "resort rice": "resort a1 sella",

    # ── Rs Gold Sambha Mansoori ───────────────────────────────────────────────
    "rs gold": "rs gold sambha mansoori",
    "rs gold samba": "rs gold sambha mansoori",
    "rs gold sambha": "rs gold sambha mansoori",
    "rs gold mansoori": "rs gold sambha mansoori",
    "r s gold": "rs gold sambha mansoori",

    # ── Shezada series ────────────────────────────────────────────────────────
    "shehzada": "shezada rice",
    "shahzada": "shezada rice",
    "shezada chawal": "shezada rice",
    "shehzada rice": "shezada rice",
    "shahzada rice": "shezada rice",
    "shezada white": "shezada white jute bag",
    "shezada jute": "shezada white jute bag",
    "shehzada white": "shezada white jute bag",
    "shahzada white": "shezada white jute bag",

    # ── Sonam Usna ────────────────────────────────────────────────────────────
    "sonam": "sonam usna",
    "sonam rice": "sonam usna",
    "sonam chawal": "sonam usna",

    # ── World Cup Rice ────────────────────────────────────────────────────────
    "world cup": "world cup rice",
    "world cup chawal": "world cup rice",

    # ── Zeeba series ──────────────────────────────────────────────────────────
    "zeba": "zeeba classic",
    "jeeba": "zeeba classic",
    "zeeba 1121 golden": "zeeba 1121 golden sella",
    "zeba 1121": "zeeba 1121 golden sella",
    "zeeba golden delight": "zeeba golden sela delight",
    "zeeba golden sella delight": "zeeba golden sela delight",
    "zeeba white delight": "zeeba white sela delight",
    "zeeba white sella delight": "zeeba white sela delight",
    "zeeba white dubar": "zeeba white sela dubar",
    "zeeba white mogra": "zeeba white sela mogra",
    "zeeba white select": "zeeba white sela select",
    "zeeba xxl": "zeeba xxxl biryani rice",
    "zeeba xxxl biryani": "zeeba xxxl biryani rice",
    "zeeba biryani": "zeeba xxxl biryani rice",
    "zeeba premium basmati": "zeeba premium basmati rice",
    "zeeba super basmati": "zeeba super basmati rice",
    "zeba classic": "zeeba classic",
    "zeba tibar": "zeeba tibar",
    "zeba kinki": "zeeba kinki",
}

# Word-level substitutions applied to each word in compound names that
# don't appear as an exact phrase in _PRODUCT_ALIASES.
# After substitution the result is re-checked against _PRODUCT_ALIASES.
# e.g. "gauri naya chawal" → "gauri new rice" → fuzzy finds "Gauri New"
_WORD_SUBS: dict[str, str] = {
    # ── Chawal variants ───────────────────────────────────────────────────────
    "chawal": "rice", "chaawal": "rice", "chavel": "rice", "chawl": "rice",
    # ── Oil / condiments ──────────────────────────────────────────────────────
    "tel": "oil", "sarson": "mustard", "sarsoon": "mustard",
    # ── Dal ───────────────────────────────────────────────────────────────────
    "daal": "dal", "dhal": "dal",
    # ── Flour ─────────────────────────────────────────────────────────────────
    "aatta": "atta",
    # ── Hindi age / variant words → English equivalents ──────────────────────
    # Enables: "gauri naya" → "gauri new", "rani sahiba purana" → "rani sahiba old"
    "naya": "new", "nayi": "new", "naye": "new",
    "purana": "old", "purani": "old", "purane": "old",
    "tuta": "broken", "toota": "broken",
}


def _normalize_product_name(name: str) -> str:
    lower = name.lower().strip()
    # 1. Exact phrase lookup
    if lower in _PRODUCT_ALIASES:
        return _PRODUCT_ALIASES[lower]
    # 2. Word-level substitution for compound names ("basmati chawal" → "basmati rice")
    words = lower.split()
    if len(words) > 1:
        substituted = " ".join(_WORD_SUBS.get(w, w) for w in words)
        if substituted != lower:
            # Re-check aliases with substituted form
            if substituted in _PRODUCT_ALIASES:
                return _PRODUCT_ALIASES[substituted]
            return substituted
    return lower


# ── OpenAI tool definitions ───────────────────────────────────────────────────

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_stock",
            "description": (
                "Get real-time stock level for a product from the shop's inventory. "
                "ALWAYS call this when user asks about stock, available quantity, "
                "kitna maal hai, stock check karo, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_name": {
                        "type": "string",
                        "description": "Product name (e.g. 'arhar dal', 'basmati rice', 'aata')",
                    }
                },
                "required": ["product_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_customer_balance",
            "description": (
                "Get a customer's current outstanding balance from the ledger. "
                "ALWAYS call this when user asks about pending, baaki, udhar, "
                "balance, kitna dena hai, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_name": {
                        "type": "string",
                        "description": "Customer name as mentioned by the user",
                    }
                },
                "required": ["customer_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_price",
            "description": (
                "Get the best available price for a product from the shop database. "
                "Checks the inventory table first (last_sale_price, last_purchase_price), "
                "then falls back to past transactions. "
                "ALWAYS call this for EVERY product in a sale whose rate_per_unit is not yet known. "
                "Call it even when the user says 'mujhe nhi pata', 'db se fetch karo', "
                "'check karo', 'I don't remember the price', etc. "
                "RESULT HANDLING — MANDATORY:\n"
                "  found=true → use returned rate. Set price_source: 'inventory'. NEVER ask user for price.\n"
                "  found=false AND ambiguous=true → rate_per_unit: null, price_source: 'ambiguous'. "
                "Do NOT ask user in chat — they pick from the product dropdown in the UI.\n"
                "  found=false (no match) → rate_per_unit: null, price_source: 'not_found'. "
                "HARD RULE: do NOT ask user for price, do NOT mention edit screen, do NOT proceed. "
                "The BACKEND automatically shows Add to Inventory and Skip & Continue buttons. "
                "NEVER set clarification_needed to a not-found message. "
                "ALWAYS include the item in transactions[]."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_name": {
                        "type": "string",
                        "description": "Product name exactly as the user said it (e.g. 'daal', 'paneer', 'rice')",
                    }
                },
                "required": ["product_name"],
            },
        },
    },
]


# ── Tool executor ─────────────────────────────────────────────────────────────

async def execute_tool(
    tool_name: str,
    tool_args: dict,
    db: AsyncSession,
    user_id: int,
) -> str:
    """Execute a tool call and return the result as a JSON string."""
    try:
        if tool_name == "get_stock":
            result = await inventory_service.get_stock(db, user_id, tool_args["product_name"])
        elif tool_name == "get_customer_balance":
            result = await inventory_service.get_customer_balance(db, user_id, tool_args["customer_name"])
        elif tool_name == "get_recent_price":
            raw_name = tool_args["product_name"]
            normalized = _normalize_product_name(raw_name)
            result = await inventory_service.get_recent_price(db, user_id, normalized)
            # Add raw_name to result so the caller knows what the user said
            if isinstance(result, dict):
                result["raw_name"] = raw_name
                result["normalized_name"] = normalized
        else:
            result = {"error": f"Unknown tool: {tool_name}"}
    except Exception as exc:
        _logger.error("Tool %s failed: %s", tool_name, exc)
        result = {"error": str(exc)}

    return json.dumps(result, ensure_ascii=False)
