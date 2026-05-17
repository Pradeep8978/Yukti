"""
yukti/signals/indicators.py
Computes all technical indicators used by Yukti on a OHLCV DataFrame.
Returns an enriched DataFrame + a flat dict of latest values for Claude context.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import logging
import numpy as np
import pandas as pd
import pandas_ta as ta

log = logging.getLogger(__name__)


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

    # Daily-only (None when timeframe="5m")
    adx:              float | None = None
    daily_support:    float | None = None
    daily_resistance: float | None = None

    # Tagged with the timeframe this snapshot was computed for so rules can
    # decide which votes are meaningful (e.g. VWAP is intraday-resetting in
    # live and on 5m candles, but degenerates to a cumulative-since-history-
    # start average on daily — meaningless as an independent vote).
    timeframe:        str = "5m"

    # ── RSI divergence (computed from recent bars, not just current row) ─
    # Bullish: price made new lower low but RSI made higher low → reversal up
    # Bearish: price made new higher high but RSI made lower high → reversal down
    rsi_bullish_divergence: bool = False
    rsi_bearish_divergence: bool = False

    # ── Candle structure of the latest bar ──────────────────────────────
    # body_ratio: |close-open| / (high-low); 0 = pure doji, 1 = full engulf
    # is_hammer: long lower wick + small body near top — bullish reversal
    # is_shooting_star: long upper wick + small body near bottom — bearish reversal
    body_ratio:       float = 1.0
    is_hammer:        bool  = False
    is_shooting_star: bool  = False

    def above_vwap(self)   -> bool: return self.close > self.vwap
    def above_ema20(self)  -> bool: return self.close > self.ema20
    def above_ema50(self)  -> bool: return self.close > self.ema50
    def rsi_overbought(self) -> bool: return self.rsi > 70
    def rsi_oversold(self)   -> bool: return self.rsi < 30
    def vwap_is_meaningful(self) -> bool:
        """True when the VWAP value carries real information.

        On daily timeframe pandas_ta.vwap returns None and we fall back to a
        cumulative-since-history-start VWAP — that's just 'price vs long-run
        average' and correlates with trend, so it shouldn't count as an
        independent confirmation vote.
        """
        return self.timeframe != "daily"


def _candle_structure(r: "pd.Series") -> tuple[float, bool, bool]:
    """Return (body_ratio, is_hammer, is_shooting_star) for one OHLC row."""
    h, l, o, c = float(r["high"]), float(r["low"]), float(r["open"]), float(r["close"])
    rng = h - l
    if rng < 1e-9:
        return 1.0, False, False
    body        = abs(c - o)
    body_ratio  = body / rng
    upper_wick  = h - max(c, o)
    lower_wick  = min(c, o) - l
    # Hammer: tiny body sitting high in the range, long lower wick
    is_hammer = (
        body_ratio < 0.40
        and lower_wick >= 2.0 * max(body, 1e-9)
        and upper_wick  < 0.5 * max(body, 1e-9)
        and (max(c, o) - l) / rng >= 0.60
    )
    # Shooting star: tiny body sitting low in the range, long upper wick
    is_shooting_star = (
        body_ratio < 0.40
        and upper_wick >= 2.0 * max(body, 1e-9)
        and lower_wick  < 0.5 * max(body, 1e-9)
        and (h - min(c, o)) / rng >= 0.60
    )
    return round(body_ratio, 3), is_hammer, is_shooting_star


def _rsi_divergence(df: pd.DataFrame, lookback: int = 10) -> tuple[bool, bool]:
    """Detect RSI/price divergence over the last `lookback` bars.

    Splits the window into two halves and compares the extreme price bar's
    RSI in the first half vs. the second half:
      Bullish: second half makes lower price low but higher RSI low  → reversal up
      Bearish: second half makes higher price high but lower RSI high → reversal down
    A 3-point RSI margin prevents noise signals.
    """
    if len(df) < lookback or "rsi" not in df.columns:
        return False, False
    recent = df.tail(lookback)
    half   = lookback // 2
    first  = recent.iloc[:half]
    second = recent.iloc[half:]
    try:
        f_lo = first["low"].idxmin()
        s_lo = second["low"].idxmin()
        bullish = (
            float(second.at[s_lo, "low"]) < float(first.at[f_lo, "low"])
            and float(second.at[s_lo, "rsi"]) > float(first.at[f_lo, "rsi"]) + 3
        )
        f_hi = first["high"].idxmax()
        s_hi = second["high"].idxmax()
        bearish = (
            float(second.at[s_hi, "high"]) > float(first.at[f_hi, "high"])
            and float(second.at[s_hi, "rsi"]) < float(first.at[f_hi, "rsi"]) - 3
        )
        return bullish, bearish
    except Exception:
        return False, False


def compute_full(df: pd.DataFrame, timeframe: str = "5m") -> pd.DataFrame:
    """Compute all indicator columns ONCE on the full OHLCV series.

    Returns the enriched DataFrame; pair with `snapshot_at()` to extract a
    snapshot at any timestamp without recomputing pandas_ta on every call.
    Backtest engines should use this to avoid O(n²) cost when stepping
    bar-by-bar through history.
    """
    df = df.copy()

    df["ema20"] = ta.ema(df["close"], length=20)
    df["ema50"] = ta.ema(df["close"], length=50)

    try:
        _vwap = None if timeframe == "daily" else ta.vwap(df["high"], df["low"], df["close"], df["volume"])
    except Exception:
        _vwap = None
    if _vwap is None or (hasattr(_vwap, "isna") and _vwap.isna().all()):
        df["tp"] = (df["high"] + df["low"] + df["close"]) / 3
        if isinstance(df.index, pd.DatetimeIndex):
            date_groups = df.index.normalize()
            df["vwap"] = (
                df.groupby(date_groups)["tp"].transform(lambda s: (s * df.loc[s.index, "volume"]).cumsum())
                / df.groupby(date_groups)["volume"].transform("cumsum")
            )
        else:
            df["vwap"] = (df["tp"] * df["volume"]).cumsum() / df["volume"].cumsum()
    else:
        df["vwap"] = _vwap

    st = ta.supertrend(df["high"], df["low"], df["close"], length=7, multiplier=3.0)
    if st is not None and "SUPERT_7_3.0" in st.columns:
        df["supertrend"] = st["SUPERT_7_3.0"]
        df["st_dir"] = st["SUPERTd_7_3.0"]
    else:
        df["supertrend"] = df["ema20"]
        df["st_dir"] = 1

    df["rsi"] = ta.rsi(df["close"], length=14)

    macd = ta.macd(df["close"], fast=12, slow=26, signal=9)
    if macd is not None:
        df["macd"] = macd["MACD_12_26_9"]
        df["macd_sig"] = macd["MACDs_12_26_9"]
        df["macd_hist"] = macd["MACDh_12_26_9"]
    else:
        df["macd"] = df["macd_sig"] = df["macd_hist"] = 0.0

    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)

    bb = ta.bbands(df["close"], length=20, std=2.0)
    if bb is not None and "BBU_20_2.0" in bb.columns:
        df["bb_upper"] = bb["BBU_20_2.0"]
        df["bb_mid"] = bb["BBM_20_2.0"]
        df["bb_lower"] = bb["BBL_20_2.0"]
    elif bb is not None:
        cols = {c.split("_")[0]: c for c in bb.columns}
        if "BBU" in cols:
            df["bb_upper"] = bb[cols["BBU"]]
            df["bb_mid"] = bb[cols["BBM"]]
            df["bb_lower"] = bb[cols["BBL"]]
        else:
            df["bb_upper"] = df["bb_mid"] = df["bb_lower"] = df["close"]
    else:
        df["bb_upper"] = df["bb_mid"] = df["bb_lower"] = df["close"]

    df["vol_sma20"] = df["volume"].rolling(20).mean()

    if timeframe == "daily":
        adx_series = ta.adx(df["high"], df["low"], df["close"], length=14)
        df["adx"] = adx_series["ADX_14"] if (adx_series is not None and "ADX_14" in adx_series.columns) else 0.0
    else:
        df["adx"] = float("nan")

    df.ffill(inplace=True)
    df.fillna(0, inplace=True)
    return df


def snapshot_at(
    df_full: pd.DataFrame,
    ts,
    swing_lookback: int = 20,
    timeframe: str = "5m",
) -> IndicatorSnapshot:
    """Build an IndicatorSnapshot from a precomputed full-series DataFrame
    at a specific timestamp (or the last row ≤ ts). No pandas_ta calls."""
    if ts not in df_full.index:
        sliced = df_full.loc[:ts]
        if sliced.empty:
            raise ValueError(f"no data at or before {ts}")
        df = sliced
    else:
        df = df_full.loc[:ts]

    r = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else r

    recent = df.tail(swing_lookback)
    highs = recent["high"].nlargest(5).values
    lows = recent["low"].nsmallest(5).values
    nearest_swing_high = float(highs[1]) if len(highs) > 1 else float(highs[0])
    nearest_swing_low = float(lows[1]) if len(lows) > 1 else float(lows[0])

    if len(df) >= 15:
        price_15_ago = float(df["close"].iloc[-15])
        delta = (float(r["close"]) - price_15_ago) / price_15_ago
        trend = "UPTREND" if delta > 0.005 else "DOWNTREND" if delta < -0.005 else "SIDEWAYS"
    else:
        trend = "SIDEWAYS"

    vol_sma = float(r.get("vol_sma20", 0)) or 1.0

    adx_value: float | None = None
    daily_support_val: float | None = None
    daily_resistance_val: float | None = None
    if timeframe == "daily":
        adx_value = float(r["adx"]) if "adx" in df.columns and pd.notna(r["adx"]) else 0.0
        sr_window = df.tail(20)
        daily_resistance_val = float(sr_window["high"].max())
        daily_support_val = float(sr_window["low"].min())

    body_ratio, is_hammer, is_shooting_star = _candle_structure(r)
    bullish_div, bearish_div = _rsi_divergence(df)

    return IndicatorSnapshot(
        close=float(r["close"]),
        high=float(r["high"]),
        low=float(r["low"]),
        open=float(r["open"]),
        volume=float(r["volume"]),
        ema20=float(r["ema20"]),
        ema50=float(r["ema50"]),
        vwap=float(r["vwap"]),
        supertrend=float(r["supertrend"]),
        supertrend_bull=int(r["st_dir"]) == 1,
        rsi=float(r["rsi"]),
        macd=float(r["macd"]),
        macd_sig=float(r["macd_sig"]),
        macd_hist=float(r["macd_hist"]),
        macd_bull=float(r["macd"]) > float(r["macd_sig"]),
        atr=float(r["atr"]),
        bb_upper=float(r["bb_upper"]),
        bb_mid=float(r["bb_mid"]),
        bb_lower=float(r["bb_lower"]),
        volume_sma20=vol_sma,
        volume_ratio=float(r["volume"]) / vol_sma,
        trend=trend,
        nearest_swing_high=nearest_swing_high,
        nearest_swing_low=nearest_swing_low,
        prev_close=float(prev["close"]),
        candle_change_pct=(float(r["close"]) - float(prev["close"])) / float(prev["close"]) * 100,
        adx=adx_value,
        daily_support=daily_support_val,
        daily_resistance=daily_resistance_val,
        timeframe=timeframe,
        rsi_bullish_divergence=bullish_div,
        rsi_bearish_divergence=bearish_div,
        body_ratio=body_ratio,
        is_hammer=is_hammer,
        is_shooting_star=is_shooting_star,
    )


def compute(df: pd.DataFrame, swing_lookback: int = 20, timeframe: str = "5m") -> IndicatorSnapshot:
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
    # ta.vwap() silently returns None for daily candles (non-intraday frequency).
    # Skip the call for daily timeframe to suppress the spurious warning.
    try:
        _vwap = None if timeframe == "daily" else ta.vwap(df["high"], df["low"], df["close"], df["volume"])
    except Exception:
        _vwap = None
    if _vwap is None or (hasattr(_vwap, "isna") and _vwap.isna().all()):
        df["tp"] = (df["high"] + df["low"] + df["close"]) / 3
        import pandas as _pd
        if isinstance(df.index, _pd.DatetimeIndex):
            # Intraday: reset VWAP each calendar day
            date_groups = df.index.normalize()
            df["vwap"] = (
                df.groupby(date_groups)["tp"].transform(lambda s: (s * df.loc[s.index, "volume"]).cumsum())
                / df.groupby(date_groups)["volume"].transform("cumsum")
            )
        else:
            # Daily / non-datetime index: single cumulative VWAP over full series
            df["vwap"] = (df["tp"] * df["volume"]).cumsum() / df["volume"].cumsum()
    else:
        df["vwap"] = _vwap

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
        # Some versions/platforms might use different column names
        upper_col = "BBU_20_2.0"
        mid_col   = "BBM_20_2.0"
        lower_col = "BBL_20_2.0"
        
        if upper_col in bb.columns:
            df["bb_upper"] = bb[upper_col]
            df["bb_mid"]   = bb[mid_col]
            df["bb_lower"] = bb[lower_col]
        else:
            # Try finding by prefix
            cols = {c.split("_")[0]: c for c in bb.columns}
            if "BBU" in cols:
                df["bb_upper"] = bb[cols["BBU"]]
                df["bb_mid"]   = bb[cols["BBM"]]
                df["bb_lower"] = bb[cols["BBL"]]
            else:
                log.warning("Indicators: BBands columns not found. Available: %s", bb.columns.tolist())
                df["bb_upper"] = df["bb_mid"] = df["bb_lower"] = df["close"]
    else:
        df["bb_upper"] = df["bb_mid"] = df["bb_lower"] = df["close"]

    # ── Volume SMA ───────────────────────────────────────────────────
    df["vol_sma20"] = df["volume"].rolling(20).mean()

    # ── ADX (daily timeframe only — noisy on 5-min) ──────────────────
    adx_value = None
    daily_support_val = None
    daily_resistance_val = None
    if timeframe == "daily":
        adx_series = ta.adx(df["high"], df["low"], df["close"], length=14)
        if adx_series is not None and "ADX_14" in adx_series.columns:
            adx_val = adx_series["ADX_14"].iloc[-1]
            adx_value = float(adx_val) if pd.notna(adx_val) else 0.0

        # Daily S/R — highest high / lowest low of last 20 sessions
        sr_window = df.tail(20)
        daily_resistance_val = float(sr_window["high"].max())
        daily_support_val    = float(sr_window["low"].min())

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

    body_ratio, is_hammer, is_shooting_star = _candle_structure(r)
    bullish_div, bearish_div = _rsi_divergence(df)

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
        adx              = adx_value,
        daily_support    = daily_support_val,
        daily_resistance = daily_resistance_val,
        timeframe        = timeframe,
        rsi_bullish_divergence = bullish_div,
        rsi_bearish_divergence = bearish_div,
        body_ratio       = body_ratio,
        is_hammer        = is_hammer,
        is_shooting_star = is_shooting_star,
    )
