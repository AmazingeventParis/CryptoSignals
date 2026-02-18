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


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    ema_fast = ema(series, fast)
    ema_slow_val = ema(series, slow)
    macd_line = ema_fast - ema_slow_val
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return {"macd": macd_line, "signal": signal_line, "histogram": histogram}


def stoch_rsi(series: pd.Series, period: int = 14, k: int = 3, d: int = 3) -> dict:
    rsi_values = rsi(series, period)
    rsi_min = rsi_values.rolling(window=period).min()
    rsi_max = rsi_values.rolling(window=period).max()
    stoch_k = ((rsi_values - rsi_min) / (rsi_max - rsi_min).replace(0, np.nan)) * 100
    stoch_k = stoch_k.rolling(window=k).mean()
    stoch_d = stoch_k.rolling(window=d).mean()
    return {"k": stoch_k, "d": stoch_d}


def adx(df: pd.DataFrame, period: int = 14) -> dict:
    high = df["high"]
    low = df["low"]
    close = df["close"]

    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr_val = tr.rolling(window=period).mean()
    plus_di = 100 * (plus_dm.rolling(window=period).mean() / atr_val.replace(0, np.nan))
    minus_di = 100 * (minus_dm.rolling(window=period).mean() / atr_val.replace(0, np.nan))

    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    adx_val = dx.rolling(window=period).mean()

    return {"adx": adx_val, "plus_di": plus_di, "minus_di": minus_di}


def obv(df: pd.DataFrame) -> pd.Series:
    close = df["close"]
    volume = df["volume"]
    direction = np.where(close > close.shift(1), 1, np.where(close < close.shift(1), -1, 0))
    obv_values = (volume * direction).cumsum()
    return pd.Series(obv_values, index=df.index)


def ichimoku(df: pd.DataFrame) -> dict:
    high = df["high"]
    low = df["low"]

    tenkan = (high.rolling(9).max() + low.rolling(9).min()) / 2
    kijun = (high.rolling(26).max() + low.rolling(26).min()) / 2
    senkou_a = ((tenkan + kijun) / 2).shift(26)
    senkou_b = ((high.rolling(52).max() + low.rolling(52).min()) / 2).shift(26)
    chikou = df["close"].shift(-26)

    return {
        "tenkan": tenkan,
        "kijun": kijun,
        "senkou_a": senkou_a,
        "senkou_b": senkou_b,
        "chikou": chikou,
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


def detect_doji(df: pd.DataFrame) -> str:
    """Detecte un doji (corps < 10% du range). Retourne bullish/bearish/neutral."""
    if len(df) < 2:
        return "none"
    candle = df.iloc[-1]
    total_range = candle["high"] - candle["low"]
    if total_range == 0:
        return "none"
    body = abs(candle["close"] - candle["open"])
    if body / total_range >= 0.10:
        return "none"
    # Doji detecte — direction basee sur la bougie precedente
    prev = df.iloc[-2]
    if prev["close"] < prev["open"]:
        return "bullish"  # doji apres bougie baissiere = retournement potentiel
    elif prev["close"] > prev["open"]:
        return "bearish"  # doji apres bougie haussiere = retournement potentiel
    return "neutral"


def detect_hammer(df: pd.DataFrame) -> str:
    """Detecte un marteau (corps en haut, longue meche basse >2x corps)."""
    if len(df) < 2:
        return "none"
    candle = df.iloc[-1]
    body = abs(candle["close"] - candle["open"])
    total_range = candle["high"] - candle["low"]
    if total_range == 0 or body == 0:
        return "none"
    upper_wick = candle["high"] - max(candle["open"], candle["close"])
    lower_wick = min(candle["open"], candle["close"]) - candle["low"]
    # Marteau classique : corps en haut, longue meche basse
    if lower_wick > body * 2 and upper_wick < body * 0.5:
        prev = df.iloc[-2]
        if prev["close"] < prev["open"]:
            return "bullish"  # marteau apres baisse = retournement haussier
        return "neutral"
    # Inverted hammer : corps en bas, longue meche haute
    if upper_wick > body * 2 and lower_wick < body * 0.5:
        prev = df.iloc[-2]
        if prev["close"] > prev["open"]:
            return "bearish"  # inverted hammer apres hausse
        return "neutral"
    return "none"


def detect_shooting_star(df: pd.DataFrame) -> str:
    """Detecte une etoile filante (corps en bas, longue meche haute >2x corps)."""
    if len(df) < 2:
        return "none"
    candle = df.iloc[-1]
    body = abs(candle["close"] - candle["open"])
    total_range = candle["high"] - candle["low"]
    if total_range == 0 or body == 0:
        return "none"
    upper_wick = candle["high"] - max(candle["open"], candle["close"])
    lower_wick = min(candle["open"], candle["close"]) - candle["low"]
    # Etoile filante : corps en bas, longue meche haute
    if upper_wick > body * 2 and lower_wick < body * 0.5:
        prev = df.iloc[-2]
        if prev["close"] > prev["open"]:
            return "bearish"  # apres hausse = signal de retournement baissier
        return "neutral"
    return "none"


def analyze_candle_context(df: pd.DataFrame, lookback: int = 5) -> dict:
    """Analyse les N dernieres bougies pour detecter resistance/support et contexte."""
    result = {
        "big_candle_resistance": False,
        "big_candle_support": False,
        "last_candle_direction": "neutral",
        "consecutive_direction": 0,
        "avg_body_ratio": 0.0,
    }
    if len(df) < lookback + 14:
        return result

    atr_values = atr(df, 14)
    atr_avg = atr_values.tail(14).mean()
    if atr_avg == 0 or pd.isna(atr_avg):
        return result

    recent = df.tail(lookback)
    current_price = df.iloc[-1]["close"]

    # Seuil grosse bougie : corps > 1.5x ATR moyen
    big_threshold = atr_avg * 1.5

    body_ratios = []
    for i in range(len(recent)):
        candle = recent.iloc[i]
        body = abs(candle["close"] - candle["open"])
        total_range = candle["high"] - candle["low"]
        body_ratios.append(body / total_range if total_range > 0 else 0)

        # Grosse bougie rouge (baissiere) pres du prix actuel = resistance
        if candle["close"] < candle["open"] and body > big_threshold:
            candle_top = max(candle["open"], candle["close"])
            candle_bottom = min(candle["open"], candle["close"])
            if candle_bottom <= current_price <= candle_top:
                result["big_candle_resistance"] = True

        # Grosse bougie verte (haussiere) pres du prix actuel = support
        if candle["close"] > candle["open"] and body > big_threshold:
            candle_top = max(candle["open"], candle["close"])
            candle_bottom = min(candle["open"], candle["close"])
            if candle_bottom <= current_price <= candle_top:
                result["big_candle_support"] = True

    result["avg_body_ratio"] = sum(body_ratios) / len(body_ratios) if body_ratios else 0

    # Derniere bougie
    last = df.iloc[-1]
    last_body = last["close"] - last["open"]
    last_range = last["high"] - last["low"]
    if last_range > 0:
        body_ratio = abs(last_body) / last_range
        if last_body > 0 and body_ratio > 0.4:
            result["last_candle_direction"] = "bullish"
        elif last_body < 0 and body_ratio > 0.4:
            result["last_candle_direction"] = "bearish"

    # Bougies consecutives dans la meme direction
    consecutive = 0
    direction = None
    for i in range(len(df) - 1, max(len(df) - 10, -1), -1):
        c = df.iloc[i]
        if c["close"] > c["open"]:
            d = "bullish"
        elif c["close"] < c["open"]:
            d = "bearish"
        else:
            break
        if direction is None:
            direction = d
            consecutive = 1
        elif d == direction:
            consecutive += 1
        else:
            break
    result["consecutive_direction"] = consecutive

    return result


def compute_all_indicators(df: pd.DataFrame, config: dict) -> dict:
    if df.empty or len(df) < 50:
        return {}

    close = df["close"]

    # --- Indicateurs de base ---
    ema_fast = ema(close, config.get("ema_fast", 20))
    ema_slow_val = ema(close, config.get("ema_slow", 50))
    ema_200_val = ema(close, 200)
    rsi_values = rsi(close, config.get("rsi_period", 14))
    atr_values = atr(df, 14)
    bb = bollinger_bands(close, 20, 2)
    vol_ratio = volume_ratio(df)
    structure = detect_market_structure(df, config.get("structure_lookback", 20))
    divergence = detect_divergence(close, rsi_values)
    engulfing = detect_engulfing(df)
    pin_bar = detect_pin_bar(df)
    doji = detect_doji(df)
    hammer = detect_hammer(df)
    shooting_star = detect_shooting_star(df)
    candle_context = analyze_candle_context(df)

    # --- Indicateurs avances ---
    macd_data = macd(close)
    stoch_rsi_data = stoch_rsi(close)
    adx_data = adx(df)
    obv_values = obv(df)
    ichimoku_data = ichimoku(df)
    vwap_values = vwap(df)

    # Divergence MACD (en plus de la divergence RSI)
    macd_divergence = detect_divergence(close, macd_data["macd"])

    return {
        # Series de base
        "ema_fast": ema_fast,
        "ema_slow": ema_slow_val,
        "ema_200": ema_200_val,
        "rsi": rsi_values,
        "atr": atr_values,
        "bb": bb,
        "volume_ratio": vol_ratio,
        "volume_sma": volume_sma(df),
        "structure": structure,
        "divergence": divergence,
        "engulfing": engulfing,
        "pin_bar": pin_bar,
        "doji": doji,
        "hammer": hammer,
        "shooting_star": shooting_star,
        "candle_context": candle_context,
        # Series avancees
        "macd": macd_data,
        "stoch_rsi": stoch_rsi_data,
        "adx": adx_data,
        "obv": obv_values,
        "ichimoku": ichimoku_data,
        "vwap": vwap_values,
        "macd_divergence": macd_divergence,
        # Dernieres valeurs
        "last_close": close.iloc[-1],
        "last_ema_fast": ema_fast.iloc[-1],
        "last_ema_slow": ema_slow_val.iloc[-1],
        "last_ema_200": ema_200_val.iloc[-1],
        "last_rsi": rsi_values.iloc[-1],
        "last_atr": atr_values.iloc[-1],
        "last_bb_upper": bb["upper"].iloc[-1],
        "last_bb_lower": bb["lower"].iloc[-1],
        "last_bb_bandwidth": bb["bandwidth"].iloc[-1],
        "last_volume_ratio": vol_ratio.iloc[-1] if not vol_ratio.empty else 0,
        "last_macd": macd_data["macd"].iloc[-1],
        "last_macd_signal": macd_data["signal"].iloc[-1],
        "last_macd_histogram": macd_data["histogram"].iloc[-1],
        "last_stoch_k": stoch_rsi_data["k"].iloc[-1],
        "last_stoch_d": stoch_rsi_data["d"].iloc[-1],
        "last_adx": adx_data["adx"].iloc[-1],
        "last_plus_di": adx_data["plus_di"].iloc[-1],
        "last_minus_di": adx_data["minus_di"].iloc[-1],
        "last_obv": obv_values.iloc[-1],
        "last_vwap": vwap_values.iloc[-1],
    }
