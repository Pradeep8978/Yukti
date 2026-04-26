"""
yukti/agents/journal.py
Structured journal reflection writer.
Generates a JSON reflection and returns a validated JournalReflection model.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Optional

import anthropic

from yukti.config import settings
from yukti.agents.rag_schemas import JournalReflection

log = logging.getLogger(__name__)


async def write_journal_entry(
  symbol:      str,
  direction:   str,
  setup_type:  str,
  entry:       float,
  stop_loss:   float,
  target:      float,
  exit_price:  float,
  exit_reason: str,
  pnl_pct:     float,
  conviction:  int,
  reasoning:   str,
  market_bias: str = "NEUTRAL",
) -> JournalReflection:
  """
  Ask Claude to write a structured JSON reflection for a closed trade.
  Returns a `JournalReflection` Pydantic model. If LLM parsing fails,
  returns a best-effort reflection with `quality_score=0`.
  """

  # Determine outcome classification for prompt injection
  if pnl_pct > 0.5:
      outcome = "WIN"
  elif pnl_pct < -0.5:
      outcome = "LOSS"
  else:
      outcome = "BREAKEVEN"

  prompt = f"""
You are a concise trading journal assistant. A trade just closed — produce a JSON object ONLY.

Input fields:
  symbol: "{symbol}"
  direction: "{direction}"
  setup_type: "{setup_type}"
  entry: {entry:.2f}
  stop_loss: {stop_loss:.2f}
  target: {target:.2f}
  exit_price: {exit_price:.2f}
  exit_reason: "{exit_reason}"
  pnl_pct: {pnl_pct:+.2f}
  conviction: {conviction}
  reasoning: "{reasoning}"
  market_bias: "{market_bias}"
  outcome: "{outcome}"

Produce JSON with the following keys:
- entry_text: short human-readable 1-2 sentence summary (first-person).
- quality_score: integer 0-10 (0 = useless, 10 = excellent insight).
- key_lesson: one short sentence describing the single most important lesson.
- setup_type: string (reuse or refine the input setup_type).
- market_regime: one of [BULLISH, BEARISH, NEUTRAL, VOLATILE] based on market_bias.
- outcome_reason: 1-2 sentence explanation why trade won/lost.
- one_actionable_lesson: one concrete action to take next time.

Return ONLY valid JSON (no surrounding text, no markdown fences). Keep values short.
""".strip()

  loop = asyncio.get_event_loop()
  client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

  try:
    response = await loop.run_in_executor(
      None,
      lambda: client.messages.create(
        model=settings.claude_model,
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
      ),
    )
    raw = response.content[0].text.strip()
  except Exception as exc:
    log.warning("Journal writer LLM call failed for %s: %s", symbol, exc)
    raw = ""

  # Try parse JSON directly
  parsed: Optional[dict] = None
  if raw:
    try:
      # strip possible fences
      if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:].strip()
      parsed = json.loads(raw)
    except Exception:
      parsed = None

  if parsed is None:
    # Fallback: create a minimal reflection using the free-text output
    entry_text = (raw.splitlines()[0] if raw else f"Trade {symbol} closed: {pnl_pct:+.2f}%")
    refl = JournalReflection(
      entry_text=str(entry_text),
      quality_score=0,
      key_lesson="",
      setup_type=setup_type,
      market_regime=market_bias,
      outcome_reason=str(exit_reason or ""),
      one_actionable_lesson="",
      created_at=datetime.utcnow(),
    )
    log.info("Journal written (fallback) for %s quality=%d", symbol, refl.quality_score)
    return refl

  # Validate and convert to JournalReflection
  try:
    refl = JournalReflection(
      entry_text=parsed.get("entry_text") or parsed.get("setup_summary") or parsed.get("journal") or "",
      quality_score=int(parsed.get("quality_score", 0)),
      key_lesson=parsed.get("key_lesson") or parsed.get("lesson") or parsed.get("one_actionable_lesson") or "",
      setup_type=parsed.get("setup_type") or setup_type,
      market_regime=parsed.get("market_regime") or market_bias,
      outcome_reason=parsed.get("outcome_reason") or parsed.get("reason") or parsed.get("why"),
      one_actionable_lesson=parsed.get("one_actionable_lesson") or parsed.get("actionable") or "",
      created_at=datetime.utcnow(),
    )
  except Exception as exc:
    log.warning("Journal parsing/validation failed for %s: %s — raw=%s", symbol, exc, raw[:300])
    refl = JournalReflection(
      entry_text=raw[:1000],
      quality_score=0,
      key_lesson="",
      setup_type=setup_type,
      market_regime=market_bias,
      outcome_reason=str(exit_reason or ""),
      one_actionable_lesson="",
      created_at=datetime.utcnow(),
    )

  log.info("Journal written for %s quality=%d", symbol, refl.quality_score)
  return refl
