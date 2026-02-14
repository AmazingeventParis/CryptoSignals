import pandas as pd
import numpy as np
from dataclasses import dataclass


@dataclass
class MarketStructure:
    trend: str  # "bullish", "bearish", "neutral"
    higher_highs: bool
    higher_lows: bool
    lower_highs: bool
    lower_lows: bool


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


def bollinger_bands(series: pd.Series, period: int = 20, std: float = 2.0) -> dict:
    middle = sma(series, period)
    std_dev = series.rolling(window=period).std()
    upper = middle + std * std_dev
    lower = middle - std * std_dev
    bandwidth = ((upper - lower) / middle) * 100
    return {
        "upper": upper,
        "middle": middle,
        "lower": lower,
        "bandwidth": bandwidth,
    }


def vwap(df: pd.DataFrame) -> pd.Series:
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    cum_volume = df["volume"].cumsum()
    cum_tp_vol = (typical_price * df["volume"]).cumsum()
    return cum_tp_vol / cum_volume.replace(0, np.nan)


def volume_sma(df: pd.DataFrame, period: int = 20) -> pd.Series:
    return sma(df["volume"], period)


def volume_ratio(df: pd.DataFrame, fast: int = 5, slow: int = 50) -> pd.Series:
    vol_fast = sma(df["volume"], fast)
    vol_slow = sma(df["volume"], slow)
    return vol_fast / vol_slow.replace(0, np.nan)


def detect_market_structure(df: pd.DataFrame, lookback: int = 20) -> MarketStructure:
    if len(df) < lookback:
        return MarketStructure("neutral", False, False, False, False)

    recent = df.tail(lookback)
    highs = recent["high"]
    lows = recent["low"]

    # Trouver les swing highs et swing lows (pivots simples)
    swing_highs = []
    swing_lows = []

    for i in range(2, len(recent) - 2):
        h = highs.iloc[i]
        if h > highs.iloc[i - 1] and h > highs.iloc[i - 2] and h > highs.iloc[i + 1] and h > highs.iloc[i + 2]:
            swing_highs.append(h)
        lo = lows.iloc[i]
        if lo < lows.iloc[i - 1] and lo < lows.iloc[i - 2] and lo < lows.iloc[i + 1] and lo < lows.iloc[i + 2]:
            swing_lows.append(lo)

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return MarketStructure("neutral", False, False, False, False)

    hh = swing_highs[-1] > swing_highs[-2]
    hl = swing_lows[-1] > swing_lows[-2]
    lh = swing_highs[-1] < swing_highs[-2]
    ll = swing_lows[-1] < swing_lows[-2]

    if hh and hl:
        trend = "bullish"
    elif lh and ll:
        trend = "bearish"
    else:
        trend = "neutral"

    return MarketStructure(trend, hh, hl, lh, ll)


def detect_divergence(price: pd.Series, indicator: pd.Series, lookback: int = 14) -> str:
    if len(price) < lookback or len(indicator) < lookback:
        return "none"

    price_recent = price.tail(lookback)
    ind_recent = indicator.tail(lookback)

    price_low1_idx = price_recent[:lookback // 2].idxmin()
    price_low2_idx = price_recent[lookback // 2:].idxmin()

    price_high1_idx = price_recent[:lookback // 2].idxmax()
    price_high2_idx = price_recent[lookback // 2:].idxmax()

    # Bullish divergence: prix fait lower low, RSI fait higher low
    if price_recent[price_low2_idx] < price_recent[price_low1_idx]:
        if ind_recent[price_low2_idx] > ind_recent[price_low1_idx]:
            return "bullish"

    # Bearish divergence: prix fait higher high, RSI fait lower high
    if price_recent[price_high2_idx] > price_recent[price_high1_idx]:
        if ind_recent[price_high2_idx] < ind_recent[price_high1_idx]:
            return "bearish"

    return "none"


def detect_engulfing(df: pd.DataFrame) -> str:
    if len(df) < 2:
        return "none"
    prev = df.iloc[-2]
    curr = df.iloc[-1]

    prev_body = prev["close"] - prev["open"]
    curr_body = curr["close"] - curr["open"]

    # Bullish engulfing
    if prev_body < 0 and curr_body > 0:
        if curr["open"] <= prev["close"] and curr["close"] >= prev["open"]:
            return "bullish"

    # Bearish engulfing
    if prev_body > 0 and curr_body < 0:
        if curr["open"] >= prev["close"] and curr["close"] <= prev["open"]:
            return "bearish"

    return "none"


def detect_pin_bar(df: pd.DataFrame) -> str:
    if len(df) < 1:
        return "none"
    candle = df.iloc[-1]
    body = abs(candle["close"] - candle["open"])
    upper_wick = candle["high"] - max(candle["open"], candle["close"])
    lower_wick = min(candle["open"], candle["close"]) - candle["low"]
    total_range = candle["high"] - candle["low"]

    if total_range == 0:
        return "none"

    # Bullish pin bar: longue meche basse
    if lower_wick > body * 2 and lower_wick > upper_wick * 2:
        return "bullish"

    # Bearish pin bar: longue meche haute
    if upper_wick > body * 2 and upper_wick > lower_wick * 2:
        return "bearish"

    return "none"


def compute_all_indicators(df: pd.DataFrame, config: dict) -> dict:
    if df.empty or len(df) < 50:
        return {}

    close = df["close"]

    ema_fast = ema(close, config.get("ema_fast", 20))
    ema_slow = ema(close, config.get("ema_slow", 50))
    rsi_values = rsi(close, config.get("rsi_period", 14))
    atr_values = atr(df, 14)
    bb = bollinger_bands(close, 20, 2)
    vol_ratio = volume_ratio(df)
    structure = detect_market_structure(df, config.get("structure_lookback", 20))
    divergence = detect_divergence(close, rsi_values)
    engulfing = detect_engulfing(df)
    pin_bar = detect_pin_bar(df)

    return {
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "rsi": rsi_values,
        "atr": atr_values,
        "bb": bb,
        "volume_ratio": vol_ratio,
        "volume_sma": volume_sma(df),
        "structure": structure,
        "divergence": divergence,
        "engulfing": engulfing,
        "pin_bar": pin_bar,
        "last_close": close.iloc[-1],
        "last_ema_fast": ema_fast.iloc[-1],
        "last_ema_slow": ema_slow.iloc[-1],
        "last_rsi": rsi_values.iloc[-1],
        "last_atr": atr_values.iloc[-1],
        "last_bb_upper": bb["upper"].iloc[-1],
        "last_bb_lower": bb["lower"].iloc[-1],
        "last_bb_bandwidth": bb["bandwidth"].iloc[-1],
        "last_volume_ratio": vol_ratio.iloc[-1] if not vol_ratio.empty else 0,
    }
