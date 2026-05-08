"""
yukti/agents/journal.py
Structured journal reflection writer.
Generates a JSON reflection and returns a validated JournalReflection model.

Provider is controlled by `settings.journal_ai_provider` ("claude" or "openai").
Gemini is intentionally excluded — Gemini quota is reserved for Arjun's trade
decisions where structured JSON output and speed matter most.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Optional

from yukti.config import settings
from yukti.agents.rag_schemas import JournalReflection

log = logging.getLogger(__name__)

_PROMPT_TEMPLATE = """
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

Produce JSON with the following keys (use these exact key names):
- `setup_summary`: short human-readable 1-2 sentence summary (first-person).
- `outcome`: one of `WIN`, `LOSS`, or `BREAKEVEN`.
- `reason`: 1-2 sentence explanation why the trade won or lost.
- `one_actionable_lesson`: one concrete action to take next time (single sentence).
- `quality_score`: integer 0-10 (0 = useless, 10 = excellent insight). If unsure, self-score and return an honest value.
- `market_regime`: one of [BULLISH, BEARISH, NEUTRAL, VOLATILE] if applicable, else null.
- `setup_type`: string (reuse or refine the input setup_type).

Return ONLY valid JSON (no surrounding text, no markdown fences). Keep values concise.
""".strip()


async def _call_claude(prompt: str) -> str:
    """Call Claude for journal writing. Returns raw text."""
    import anthropic
    loop = asyncio.get_event_loop()
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    response = await loop.run_in_executor(
        None,
        lambda: client.messages.create(
            model=settings.claude_model,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        ),
    )
    return response.content[0].text.strip()


async def _call_openai(prompt: str) -> str:
    """Call OpenAI for journal writing. Returns raw text."""
    import openai as _openai
    loop = asyncio.get_event_loop()
    client = _openai.OpenAI(api_key=settings.openai_api_key)
    response = await loop.run_in_executor(
        None,
        lambda: client.chat.completions.create(
            model=settings.openai_model,
            max_tokens=settings.openai_max_tokens,
            temperature=settings.openai_temperature,
            messages=[{"role": "user", "content": prompt}],
        ),
    )
    return (response.choices[0].message.content or "").strip()


async def _llm_call(prompt: str) -> str:
    """Route the journal LLM call through journal_ai_provider (never Gemini)."""
    provider = getattr(settings, "journal_ai_provider", "claude")
    if provider == "openai":
        return await _call_openai(prompt)
    return await _call_claude(prompt)


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
  Write a structured JSON reflection for a closed trade via journal_ai_provider.
  Returns a `JournalReflection` Pydantic model. If LLM parsing fails,
  returns a best-effort reflection with `quality_score=0`.
  """
  if pnl_pct > 0.5:
      outcome = "WIN"
  elif pnl_pct < -0.5:
      outcome = "LOSS"
  else:
      outcome = "BREAKEVEN"

  prompt = _PROMPT_TEMPLATE.format(
      symbol=symbol,
      direction=direction,
      setup_type=setup_type,
      entry=entry,
      stop_loss=stop_loss,
      target=target,
      exit_price=exit_price,
      exit_reason=exit_reason,
      pnl_pct=pnl_pct,
      conviction=conviction,
      reasoning=reasoning,
      market_bias=market_bias,
      outcome=outcome,
  )

  raw = ""
  try:
      raw = await _llm_call(prompt)
  except Exception as exc:
      log.warning("Journal writer LLM call failed for %s: %s", symbol, exc)

  # Try parse JSON directly
  parsed: Optional[dict] = None
  if raw:
    try:
      if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:].strip()
      parsed = json.loads(raw)
    except Exception:
      parsed = None

  if parsed is None:
    setup_summary = (raw.splitlines()[0] if raw else f"Trade {symbol} closed: {pnl_pct:+.2f}%")
    refl = JournalReflection(
      setup_summary=str(setup_summary),
      outcome=outcome,
      reason=str(exit_reason or ""),
      one_actionable_lesson="",
      quality_score=0,
      market_regime=market_bias,
      setup_type=setup_type,
      created_at=datetime.utcnow(),
    )
    log.info("Journal written (fallback) for %s quality=%d", symbol, refl.quality_score)
    return refl

  try:
    setup_summary = parsed.get("setup_summary") or parsed.get("entry_text") or parsed.get("summary") or ""
    outcome_val = (parsed.get("outcome") or parsed.get("result") or outcome or "BREAKEVEN")
    reason_val = parsed.get("reason") or parsed.get("outcome_reason") or parsed.get("why") or ""
    actionable = parsed.get("one_actionable_lesson") or parsed.get("actionable") or parsed.get("one_actionable") or ""
    raw_quality = parsed.get("quality_score")
    if raw_quality is None:
        length_score = min(6, max(0, int(len(setup_summary) / 40)))
        bonus = 0
        if reason_val:
            bonus += 1
        if actionable:
            bonus += 1
        qual = min(10, length_score + bonus)
    else:
        try:
            qual = int(raw_quality)
        except Exception:
            qual = 0

    refl = JournalReflection(
      setup_summary=str(setup_summary),
      outcome=str(outcome_val),
      reason=str(reason_val),
      one_actionable_lesson=str(actionable),
      quality_score=qual,
      market_regime=parsed.get("market_regime") or market_bias,
      setup_type=parsed.get("setup_type") or setup_type,
      created_at=datetime.utcnow(),
    )
  except Exception as exc:
    log.warning("Journal parsing/validation failed for %s: %s — raw=%s", symbol, exc, raw[:300])
    refl = JournalReflection(
      setup_summary=raw[:1000],
      outcome=outcome,
      reason=str(exit_reason or ""),
      one_actionable_lesson="",
      quality_score=0,
      market_regime=market_bias,
      setup_type=setup_type,
      created_at=datetime.utcnow(),
    )

  log.info(
      "Journal written for %s via %s quality=%d",
      symbol,
      getattr(settings, "journal_ai_provider", "claude"),
      refl.quality_score,
  )
  return refl
