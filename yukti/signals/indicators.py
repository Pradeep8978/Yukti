"""
yukti/signals/indicators.py
Computes all technical indicators used by Yukti on a OHLCV DataFrame.
Returns an enriched DataFrame + a flat dict of latest values for Claude context.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
import pandas_ta as ta


@dataclass
class IndicatorSnapshot:
    """Latest values of all indicators — passed to context builder."""

    # Price
    close:    float
    high:     float
    low:      float
    open:     float
    volume:   float

    # Trend
    ema20:    float
    ema50:    float
    vwap:     float
    supertrend: float
    supertrend_bull: bool

    # Momentum
    rsi:      float
    macd:     float
    macd_sig: float
    macd_hist: float
    macd_bull: bool    # macd > signal

    # Volatility
    atr:      float
    bb_upper: float
    bb_mid:   float
    bb_lower: float

    # Volume
    volume_sma20: float
    volume_ratio: float  # volume / volume_sma20

    # Market structure (derived from recent candles)
    trend:          str   # UPTREND | DOWNTREND | SIDEWAYS
    nearest_swing_high: float
    nearest_swing_low:  float
    prev_close:     float
    candle_change_pct: float

    def above_vwap(self)   -> bool: return self.close > self.vwap
    def above_ema20(self)  -> bool: return self.close > self.ema20
    def above_ema50(self)  -> bool: return self.close > self.ema50
    def rsi_overbought(self) -> bool: return self.rsi > 70
    def rsi_oversold(self)   -> bool: return self.rsi < 30


def compute(df: pd.DataFrame, swing_lookback: int = 20) -> IndicatorSnapshot:
    """
    Compute all indicators on a OHLCV dataframe and return the latest snapshot.

    Args:
        df: DataFrame with columns [open, high, low, close, volume], ascending time index.
            At least 60 rows recommended (50-period EMA needs history).
        swing_lookback: number of candles to use for swing high/low detection.

    Returns:
        IndicatorSnapshot with latest values.
    """
    df = df.copy()

    # ── Moving averages ──────────────────────────────────────────────
    df["ema20"] = ta.ema(df["close"], length=20)
    df["ema50"] = ta.ema(df["close"], length=50)

    # ── VWAP (intraday — resets on each trading day) ─────────────────
    try:
        df["vwap"] = ta.vwap(df["high"], df["low"], df["close"], df["volume"])
    except Exception:
        # Fallback: simple (H+L+C)/3 × volume weighted avg
        df["tp"] = (df["high"] + df["low"] + df["close"]) / 3
        df["vwap"] = (df["tp"] * df["volume"]).cumsum() / df["volume"].cumsum()

    # ── Supertrend ───────────────────────────────────────────────────
    st = ta.supertrend(df["high"], df["low"], df["close"], length=7, multiplier=3.0)
    if st is not None and "SUPERT_7_3.0" in st.columns:
        df["supertrend"] = st["SUPERT_7_3.0"]
        df["st_dir"]     = st["SUPERTd_7_3.0"]   # 1 = bullish, -1 = bearish
    else:
        df["supertrend"] = df["ema20"]
        df["st_dir"]     = 1

    # ── RSI ──────────────────────────────────────────────────────────
    df["rsi"] = ta.rsi(df["close"], length=14)

    # ── MACD ─────────────────────────────────────────────────────────
    macd = ta.macd(df["close"], fast=12, slow=26, signal=9)
    if macd is not None:
        df["macd"]     = macd["MACD_12_26_9"]
        df["macd_sig"] = macd["MACDs_12_26_9"]
        df["macd_hist"]= macd["MACDh_12_26_9"]
    else:
        df["macd"] = df["macd_sig"] = df["macd_hist"] = 0.0

    # ── ATR ──────────────────────────────────────────────────────────
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)

    # ── Bollinger Bands ──────────────────────────────────────────────
    bb = ta.bbands(df["close"], length=20, std=2.0)
    if bb is not None:
        df["bb_upper"] = bb["BBU_20_2.0"]
        df["bb_mid"]   = bb["BBM_20_2.0"]
        df["bb_lower"] = bb["BBL_20_2.0"]
    else:
        df["bb_upper"] = df["bb_mid"] = df["bb_lower"] = df["close"]

    # ── Volume SMA ───────────────────────────────────────────────────
    df["vol_sma20"] = df["volume"].rolling(20).mean()

    # ── Fill NaN with forward fill then zero ────────────────────────
    df.ffill(inplace=True)
    df.fillna(0, inplace=True)

    # ── Latest row ───────────────────────────────────────────────────
    r     = df.iloc[-1]
    prev  = df.iloc[-2] if len(df) > 1 else r

    # ── Swing high / low (last N candles) ────────────────────────────
    recent = df.tail(swing_lookback)
    highs  = recent["high"].nlargest(5).values
    lows   = recent["low"].nsmallest(5).values
    nearest_swing_high = float(highs[1]) if len(highs) > 1 else float(highs[0])
    nearest_swing_low  = float(lows[1])  if len(lows)  > 1 else float(lows[0])

    # ── Trend from 15-candle price comparison ─────────────────────────
    if len(df) >= 15:
        price_15_ago = float(df["close"].iloc[-15])
        delta = (float(r["close"]) - price_15_ago) / price_15_ago
        trend = "UPTREND" if delta > 0.005 else "DOWNTREND" if delta < -0.005 else "SIDEWAYS"
    else:
        trend = "SIDEWAYS"

    vol_sma = float(r["vol_sma20"]) or 1.0

    return IndicatorSnapshot(
        close    = float(r["close"]),
        high     = float(r["high"]),
        low      = float(r["low"]),
        open     = float(r["open"]),
        volume   = float(r["volume"]),
        ema20    = float(r["ema20"]),
        ema50    = float(r["ema50"]),
        vwap     = float(r["vwap"]),
        supertrend      = float(r["supertrend"]),
        supertrend_bull = int(r["st_dir"]) == 1,
        rsi      = float(r["rsi"]),
        macd     = float(r["macd"]),
        macd_sig = float(r["macd_sig"]),
        macd_hist= float(r["macd_hist"]),
        macd_bull= float(r["macd"]) > float(r["macd_sig"]),
        atr      = float(r["atr"]),
        bb_upper = float(r["bb_upper"]),
        bb_mid   = float(r["bb_mid"]),
        bb_lower = float(r["bb_lower"]),
        volume_sma20  = vol_sma,
        volume_ratio  = float(r["volume"]) / vol_sma,
        trend         = trend,
        nearest_swing_high = nearest_swing_high,
        nearest_swing_low  = nearest_swing_low,
        prev_close         = float(prev["close"]),
        candle_change_pct  = (float(r["close"]) - float(prev["close"])) / float(prev["close"]) * 100,
    )
