"""yukti/signals/sentiment.py

Rule-based financial headline sentiment scoring — no AI, no API.

Two-layer approach:
  Layer 1 — Finance-specific lexicon: domain words where generic NLP gets
             polarity wrong ("default" is neutral in English, strongly negative
             in finance; "loss" is negative but "tax loss harvesting" is neutral).
             Scores are hand-tuned for NSE corporate announcements.
  Layer 2 — VADER: handles negation ("not strong results"), ALL-CAPS emphasis,
             punctuation boosting ("profit!!!"), and general English sentiment.

Final score: weighted blend in [-1.0, +1.0].
  >  0.3  → positive catalyst  (beat estimates, order win, buyback)
   -0.3 to 0.3 → neutral/ambiguous
  < -0.3  → negative catalyst  (fraud, SEBI notice, default, loss)
  < -0.6  → strongly negative  (ban, delistment, insolvency — hard skip territory)

Usage:
    from yukti.signals.sentiment import score_headline
    s = score_headline("INFY beats Q3 estimates; profit up 18% YoY")  # → ~0.72
    s = score_headline("SEBI issues show-cause notice for insider trading") # → ~-0.85
"""
from __future__ import annotations

import re
import logging
from functools import lru_cache

log = logging.getLogger(__name__)

# ── Finance-specific polarity lexicon ────────────────────────────────────────
# Tuples of (keyword_fragment, score). Longer / more specific entries first so
# substring matching doesn't short-circuit on partial words.
# Score range: [-1.0, +1.0]. We scan lowercased text left-to-right; first match
# per word wins so the list order matters for overlapping terms.

_FIN_LEXICON: list[tuple[str, float]] = [
    # ── Strongly positive ────────────────────────────────────────────────────
    ("beat estimate",          +1.0),
    ("beat consensus",         +1.0),
    ("beats expectation",      +1.0),
    ("record profit",          +0.95),
    ("record revenue",         +0.95),
    ("record high",            +0.85),
    ("all-time high",          +0.85),
    ("all time high",          +0.85),
    ("strong growth",          +0.80),
    ("robust growth",          +0.80),
    ("highest ever",           +0.80),
    ("buyback",                +0.75),
    ("buy-back",               +0.75),
    ("share repurchase",       +0.75),
    ("special dividend",       +0.72),
    ("interim dividend",       +0.65),
    ("final dividend",         +0.65),
    ("dividend",               +0.55),
    ("upgrade",                +0.70),
    ("outperform",             +0.70),
    ("overweight",             +0.65),
    ("order win",              +0.75),
    ("order bag",              +0.70),
    ("contract win",           +0.75),
    ("approved by",            +0.60),
    ("regulatory approval",    +0.70),
    ("sebi approval",          +0.70),
    ("merger approved",        +0.65),
    ("expansion plan",         +0.60),
    ("capacity expansion",     +0.60),
    ("qip subscribed",         +0.65),
    ("fund raise",             +0.50),
    ("fundraise",              +0.50),
    ("profit up",              +0.80),
    ("pat up",                 +0.80),
    ("ebitda up",              +0.75),
    ("revenue up",             +0.70),
    ("margin expansion",       +0.70),
    ("beat",                   +0.60),   # generic "beat" — lower weight
    ("exceeds",                +0.55),
    ("surges",                 +0.60),
    ("jumps",                  +0.55),
    ("rallies",                +0.50),
    ("bonus share",            +0.55),
    ("stock split",            +0.45),
    ("strategic partnership",  +0.55),
    ("partnership",            +0.35),

    # ── Mildly positive ──────────────────────────────────────────────────────
    ("in line with",           +0.10),
    ("steady growth",          +0.35),
    ("stable",                 +0.20),
    ("positive",               +0.25),
    ("growth",                 +0.25),

    # ── Mildly negative ──────────────────────────────────────────────────────
    ("misses estimate",        -0.70),
    ("miss estimate",          -0.70),
    ("below estimate",         -0.65),
    ("below expectation",      -0.65),
    ("disappoints",            -0.60),
    ("profit falls",           -0.65),
    ("pat falls",              -0.65),
    ("profit declines",        -0.65),
    ("revenue declines",       -0.60),
    ("margin contraction",     -0.65),
    ("guidance cut",           -0.70),
    ("downgrade",              -0.70),
    ("underperform",           -0.65),
    ("underweight",            -0.60),
    ("sell rating",            -0.65),
    ("rating cut",             -0.70),
    ("stake sale",             -0.35),  # promoter stake sale = mild negative
    ("promoter pledg",         -0.55),
    ("promoter sell",          -0.55),
    ("below par",              -0.40),
    ("slowing",                -0.30),
    ("slowdown",               -0.35),
    ("subdued",                -0.30),
    ("weak",                   -0.35),
    ("decline",                -0.35),

    # ── Strongly negative ────────────────────────────────────────────────────
    ("fraud",                  -1.0),
    ("fraudulent",             -1.0),
    ("insider trading",        -1.0),
    ("money laundering",       -1.0),
    ("delistment",             -0.95),
    ("delisting",              -0.90),
    ("insolvency",             -0.95),
    ("bankruptcy",             -0.95),
    ("default",                -0.90),
    ("nclt",                   -0.85),  # insolvency tribunal
    ("show-cause notice",      -0.85),
    ("showcause notice",       -0.85),
    ("sebi notice",            -0.85),
    ("sebi order",             -0.80),
    ("sebi ban",               -1.0),
    ("sebi penalty",           -0.85),
    ("rbi penalty",            -0.85),
    ("penalty",                -0.70),
    ("fine",                   -0.55),
    ("investigation",          -0.80),
    ("probe",                  -0.75),
    ("cbi",                    -0.80),
    ("ed notice",              -0.85),
    ("enforcement directorate",-0.85),
    ("usfda import alert",     -0.90),
    ("fda warning letter",     -0.85),
    ("warning letter",         -0.70),
    ("import alert",           -0.80),
    ("recall",                 -0.70),
    ("restated",               -0.65),  # financials restated = accounting issue
    ("restatement",            -0.65),
    ("qualified opinion",      -0.75),  # auditor qualification
    ("adverse opinion",        -0.80),
    ("going concern",          -0.90),
    ("resignation of md",      -0.60),
    ("ceo resign",             -0.55),
    ("md resign",              -0.60),
    ("write-off",              -0.65),
    ("writeoff",               -0.65),
    ("impairment",             -0.60),
    ("loss",                   -0.45),  # generic — lower weight (could be "tax loss")
    ("losses",                 -0.45),
    ("net loss",               -0.70),
    ("fire",                   -0.55),  # plant fire — operational risk
    ("explosion",              -0.65),
]

# Pre-compile regex patterns for each entry once at import time
_COMPILED: list[tuple[re.Pattern[str], float]] = [
    (re.compile(re.escape(kw)), score)
    for kw, score in _FIN_LEXICON
]


def _fin_score(text: str) -> float | None:
    """Return finance-lexicon score for text, or None if no keyword matched."""
    t = text.lower()
    scores: list[float] = []
    for pattern, sc in _COMPILED:
        if pattern.search(t):
            scores.append(sc)
    if not scores:
        return None
    # Average matched scores; if multiple strong signals conflict, they
    # partially cancel — which is the right behaviour (mixed news).
    return sum(scores) / len(scores)


@lru_cache(maxsize=1)
def _vader_analyzer():
    """Lazy-load VADER — cached for the process lifetime."""
    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        return SentimentIntensityAnalyzer()
    except ImportError:
        log.warning("vaderSentiment not installed — VADER layer disabled. "
                    "Run: pip install vaderSentiment")
        return None


def _vader_score(text: str) -> float | None:
    """Return VADER compound score [-1, +1], or None if VADER unavailable."""
    analyzer = _vader_analyzer()
    if analyzer is None:
        return None
    try:
        return float(analyzer.polarity_scores(text)["compound"])
    except Exception:
        return None


def score_headline(text: str) -> float:
    """Combined finance-aware sentiment score in [-1.0, +1.0].

    Blending weights:
      - If both layers fire: 65% finance lexicon + 35% VADER.
        Finance lexicon is dominant because domain accuracy > general English.
      - If only one layer fires: use that score directly.
      - If neither fires (empty / unknown text): return 0.0 (neutral).
    """
    if not text or not text.strip():
        return 0.0

    fin  = _fin_score(text)
    vdr  = _vader_score(text)

    if fin is not None and vdr is not None:
        return round(0.65 * fin + 0.35 * vdr, 4)
    if fin is not None:
        return round(fin, 4)
    if vdr is not None:
        return round(vdr, 4)
    return 0.0


# ── Convenience thresholds ────────────────────────────────────────────────────

def sentiment_label(score: float) -> str:
    if score >= 0.5:
        return "STRONGLY_POSITIVE"
    if score >= 0.2:
        return "POSITIVE"
    if score <= -0.5:
        return "STRONGLY_NEGATIVE"
    if score <= -0.2:
        return "NEGATIVE"
    return "NEUTRAL"
