"""
yukti/metrics.py
Prometheus metrics for Yukti.
Exposed at GET /metrics — scraped by Prometheus every 15s.
Visualised in Grafana.
"""
from __future__ import annotations

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    Info,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

# ── Trade metrics ─────────────────────────────────────────────────────────────

trades_total = Counter(
    "yukti_trades_total",
    "Total trades placed since startup",
    ["direction", "setup_type", "outcome"],   # labels
)

trades_open = Gauge(
    "yukti_trades_open",
    "Current number of open positions",
)

# ── P&L ───────────────────────────────────────────────────────────────────────

daily_pnl_pct = Gauge(
    "yukti_daily_pnl_pct",
    "Today's realised P&L as a percentage of account",
)

account_value = Gauge(
    "yukti_account_value_inr",
    "Current account value in INR",
)

consecutive_losses = Gauge(
    "yukti_consecutive_losses",
    "Current consecutive loss streak (0 if winning)",
)

win_rate_last_10 = Gauge(
    "yukti_win_rate_last_10",
    "Win rate over the last 10 closed trades (0-1)",
)

# ── Signals ───────────────────────────────────────────────────────────────────

signals_scanned = Counter(
    "yukti_signals_scanned_total",
    "Total number of symbol-candle pairs scanned",
)

signals_skipped = Counter(
    "yukti_signals_skipped_total",
    "Signals where Claude or a gate decided to skip",
    ["reason"],
)

# ── Claude API ────────────────────────────────────────────────────────────────

claude_latency = Histogram(
    "yukti_claude_latency_seconds",
    "Latency of each Claude API call",
    buckets=[0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 30.0],
)

claude_errors = Counter(
    "yukti_claude_errors_total",
    "Number of Claude API errors (non-JSON, timeouts, etc.)",
)

claude_cost_usd = Counter(
    "yukti_claude_cost_usd_total",
    "Approximate Claude API cost in USD (based on token counts)",
)

# ── DhanHQ API ────────────────────────────────────────────────────────────────

dhan_requests = Counter(
    "yukti_dhan_requests_total",
    "Total DhanHQ API requests",
    ["endpoint", "status"],   # status: ok | error
)

dhan_latency = Histogram(
    "yukti_dhan_latency_seconds",
    "Latency of DhanHQ API calls",
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0],
)

# ── System health ─────────────────────────────────────────────────────────────

agent_halted = Gauge(
    "yukti_agent_halted",
    "1 if the agent's kill switch is active, 0 otherwise",
)

signal_loop_last_run = Gauge(
    "yukti_signal_loop_last_run_timestamp",
    "Unix timestamp of the last completed signal loop cycle",
)

version_info = Info(
    "yukti_version",
    "Yukti version and build info",
)
version_info.info({"version": "0.1.0", "mode": "paper"})

# ── Canary & Self-learning metrics
canary_requests = Counter(
    "yukti_canary_requests_total",
    "Total requests routed to a canary model",
)

canary_successes = Counter(
    "yukti_canary_successes_total",
    "Total successful canary responses",
)

canary_failures = Counter(
    "yukti_canary_failures_total",
    "Total failed canary responses",
)

canary_promotions = Counter(
    "yukti_canary_promotions_total",
    "Number of times a candidate was promoted to canary",
)

canary_rollbacks = Counter(
    "yukti_canary_rollbacks_total",
    "Number of canary rollbacks performed",
)

self_learning_runs = Counter(
    "yukti_self_learning_runs_total",
    "Number of self-learning loop runs",
)

self_learning_failures = Counter(
    "yukti_self_learning_failures_total",
    "Number of self-learning loop failures",
)


# ── RAG retrieval metrics ───────────────────────────────────────────────────
rag_retrieval_count = Counter(
    "yukti_rag_retrievals_total",
    "Number of RAG retrieval operations performed",
)

rag_avg_similarity = Gauge(
    "yukti_rag_avg_similarity",
    "Average similarity of last RAG retrieval candidates",
)

rag_quality_score_avg = Gauge(
    "yukti_rag_quality_score_avg",
    "Average journal reflection quality score for retrievals",
)

rag_retrieved_wins = Counter(
    "yukti_rag_retrieved_wins_total",
    "Number of retrieved journal entries that were winning trades",
)


# ── FastAPI endpoint helper ───────────────────────────────────────────────────

def metrics_response() -> tuple[bytes, str]:
    """Return (body, content_type) for the /metrics endpoint."""
    return generate_latest(), CONTENT_TYPE_LATEST


# ── Helpers called by the agent ───────────────────────────────────────────────

def record_trade_opened(direction: str, setup_type: str) -> None:
    trades_open.inc()


def record_trade_closed(direction: str, setup_type: str, won: bool) -> None:
    trades_open.dec()
    outcome = "win" if won else "loss"
    trades_total.labels(
        direction  = direction,
        setup_type = setup_type,
        outcome    = outcome,
    ).inc()


def record_skip(reason: str) -> None:
    signals_skipped.labels(reason=reason or "unknown").inc()


def estimate_claude_cost(input_tokens: int, output_tokens: int) -> float:
    """
    Estimate Claude Sonnet 4.6 cost per call.
    Sonnet 4.6: $3/$15 per MTok (input/output) as of mid-2025.
    """
    cost = (input_tokens / 1_000_000) * 3.0 + (output_tokens / 1_000_000) * 15.0
    claude_cost_usd.inc(cost)
    return cost
