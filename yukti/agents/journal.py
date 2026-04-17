"""
yukti/agents/journal.py
Writes a 4-sentence reflective journal entry after every closed trade.
The text is stored in PostgreSQL with a pgvector embedding for semantic retrieval.
"""
from __future__ import annotations

import logging

import anthropic

from yukti.config import settings

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
) -> str:
    """
    Ask Claude to write a reflective 4-sentence journal entry for a closed trade.
    Returns the journal text string.

    The text is:
      1. What the setup was and why I entered.
      2. What happened during the trade.
      3. Why it worked / failed (specific and honest).
      4. One concrete change for next time.
    """
    prompt = f"""A trade just closed. Write a 4-sentence reflective journal entry.

Trade details:
  Symbol      : {symbol}
  Direction   : {direction}
  Setup       : {setup_type}
  Entry       : ₹{entry:.2f}  |  SL ₹{stop_loss:.2f}  |  Target ₹{target:.2f}
  Exit        : ₹{exit_price:.2f}  ({exit_reason})
  P&L         : {pnl_pct:+.2f}%
  Conviction  : {conviction}/10

Reasoning at entry:
  "{reasoning}"

Write exactly 4 sentences:
  1. What the setup was and why I entered it.
  2. What happened during the trade.
  3. Why it worked (or failed) — be specific and honest.
  4. One concrete thing I will do differently next time in a similar setup.

First person. Direct. No filler."""

    client   = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    response = client.messages.create(
        model      = settings.claude_model,
        max_tokens = 350,
        messages   = [{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()
    log.debug("Journal written for %s %s (%.2f%%)", symbol, direction, pnl_pct)
    return text
