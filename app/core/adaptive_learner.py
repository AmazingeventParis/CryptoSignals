"""
Adaptive Learner : moteur d'apprentissage adaptatif par bot (V1/V2/V3).
8 dimensions d'apprentissage avec reponses graduees,
detection de decay, fenetres glissantes.
"""
import asyncio
import logging
from datetime import datetime

from app.database import (
    insert_trade_context,
    upsert_learning_weight,
    update_learning_weight_stats,
    get_all_learning_weights,
    get_trade_context_window,
)

logger = logging.getLogger(__name__)

# 8 dimensions d'apprentissage
DIMENSIONS = [
    "setup_type",    # breakout, retest, divergence, ema_bounce, momentum
    "symbol",        # les 9 paires
    "mode",          # scalping, swing
    "regime",        # trending, ranging, volatile
    "hour_group",    # asian, european, us
    "score_range",   # 50-59, 60-69, 70-79, 80+
    "direction",     # long, short
    "mtf_confluence", # negative, zero, positive
]

CACHE_REFRESH_SECONDS = 120  # Refresh cache toutes les 2 minutes
MIN_TRADES_FOR_MODIFIER = 5
MODIFIER_CAP_MIN = -20
MODIFIER_CAP_MAX = 10


def _hour_to_group(hour: int) -> str:
    if 0 <= hour < 8:
        return "asian"
    elif 8 <= hour < 16:
        return "european"
    else:
        return "us"


def _score_to_range(score: float) -> str:
    if score >= 80:
        return "80+"
    elif score >= 70:
        return "70-79"
    elif score >= 60:
        return "60-69"
    else:
        return "50-59"


def _mtf_to_label(mtf: float) -> str:
    if mtf is None:
        return "zero"
    if mtf > 0:
        return "positive"
    elif mtf < 0:
        return "negative"
    return "zero"


class AdaptiveLearner:
    def __init__(self, bot_version: str = "V2"):
        self.bot_version = bot_version
        self._cache: dict[str, dict] = {}  # key="dim:val" -> weight row
        self._last_refresh: float = 0
        self._lock = asyncio.Lock()

    async def refresh_cache(self):
        """Charge les poids depuis la DB en cache memoire."""
        try:
            weights = await get_all_learning_weights(self.bot_version)
            new_cache = {}
            for w in weights:
                key = f"{w['dimension']}:{w['dimension_value']}"
                new_cache[key] = w
            self._cache = new_cache
            self._last_refresh = asyncio.get_event_loop().time()
        except Exception as e:
            logger.error(f"[{self.bot_version}] AdaptiveLearner refresh error: {e}")

    async def _ensure_cache(self):
        now = asyncio.get_event_loop().time()
        if now - self._last_refresh > CACHE_REFRESH_SECONDS:
            await self.refresh_cache()

    def get_total_modifier(self, signal_context: dict) -> tuple[int, list[str]]:
        """
        Calcule le modificateur total a appliquer au score final.
        signal_context contient: setup_type, symbol, mode, regime, hour_utc, score, direction, mtf_confluence
        Retourne (modifier, reasons).
        """
        total = 0
        reasons = []

        mappings = {
            "setup_type": signal_context.get("setup_type", ""),
            "symbol": signal_context.get("symbol", ""),
            "mode": signal_context.get("mode", ""),
            "regime": signal_context.get("regime", ""),
            "hour_group": _hour_to_group(signal_context.get("hour_utc", 12)),
            "score_range": _score_to_range(signal_context.get("score", 0)),
            "direction": signal_context.get("direction", ""),
            "mtf_confluence": _mtf_to_label(signal_context.get("mtf_confluence")),
        }

        for dim, val in mappings.items():
            if not val:
                continue
            key = f"{dim}:{val}"
            w = self._cache.get(key)
            if not w:
                continue
            mod = w.get("weight_modifier", 0)
            if mod != 0:
                total += mod
                reasons.append(f"{dim}={val} {mod:+.0f}pts (WR {w.get('win_rate_7d', 0):.0f}%)")

        # Cap le modificateur total
        total = max(MODIFIER_CAP_MIN, min(MODIFIER_CAP_MAX, total))
        return int(total), reasons

    async def record_trade_context(self, context: dict):
        """
        Enregistre le contexte complet d'un trade ferme et met a jour les poids.
        """
        async with self._lock:
            try:
                # 1. Inserer le contexte dans trade_context
                await insert_trade_context(context)

                # 2. Mettre a jour les learning_weights pour chaque dimension
                is_win = context.get("result") == "win"
                pnl = context.get("pnl_usd", 0)

                dims = {
                    "setup_type": context.get("setup_type", ""),
                    "symbol": context.get("symbol", ""),
                    "mode": context.get("mode", ""),
                    "regime": context.get("market_regime", ""),
                    "hour_group": _hour_to_group(context.get("hour_utc", 12)),
                    "score_range": _score_to_range(context.get("final_score", 0)),
                    "direction": context.get("direction", ""),
                    "mtf_confluence": _mtf_to_label(context.get("mtf_confluence")),
                }

                for dim, val in dims.items():
                    if not val:
                        continue
                    await upsert_learning_weight(dim, val, is_win, pnl, self.bot_version)

                # 3. Recalculer les win rates et modifiers
                await self._recalculate_weights()

                # 4. Refresh le cache
                await self.refresh_cache()

            except Exception as e:
                logger.error(f"[{self.bot_version}] AdaptiveLearner record error: {e}", exc_info=True)

    async def _recalculate_weights(self):
        """Recalcule les weight_modifier pour toutes les dimensions avec fenetres glissantes."""
        try:
            # Recuperer les trades des 7j et 30j
            trades_7d = await get_trade_context_window(self.bot_version, days=7)
            trades_30d = await get_trade_context_window(self.bot_version, days=30)
            trades_all = await get_trade_context_window(self.bot_version, days=0, limit=2000)

            # Calculer win rates par dimension + valeur
            for dim in DIMENSIONS:
                values_set = set()
                for t in trades_all:
                    val = self._extract_dimension_value(t, dim)
                    if val:
                        values_set.add(val)

                for val in values_set:
                    wr_7d = self._calc_win_rate(trades_7d, dim, val)
                    wr_30d = self._calc_win_rate(trades_30d, dim, val)
                    wr_all = self._calc_win_rate(trades_all, dim, val)
                    sample = self._count_trades(trades_all, dim, val)

                    # Calcul du modifier gradue
                    modifier = self._compute_modifier(wr_7d, wr_30d, sample)
                    confidence = min(1.0, sample / 20)

                    await update_learning_weight_stats(
                        dim, val, self.bot_version,
                        modifier, confidence,
                        wr_7d, wr_30d, wr_all,
                    )

        except Exception as e:
            logger.error(f"[{self.bot_version}] Recalculate weights error: {e}", exc_info=True)

    def _extract_dimension_value(self, trade: dict, dim: str) -> str:
        if dim == "setup_type":
            return trade.get("setup_type", "")
        elif dim == "symbol":
            return trade.get("symbol", "")
        elif dim == "mode":
            return trade.get("mode", "")
        elif dim == "regime":
            return trade.get("market_regime", "")
        elif dim == "hour_group":
            return _hour_to_group(trade.get("hour_utc", 12))
        elif dim == "score_range":
            return _score_to_range(trade.get("final_score", 0) or 0)
        elif dim == "direction":
            return trade.get("direction", "")
        elif dim == "mtf_confluence":
            return _mtf_to_label(trade.get("mtf_confluence"))
        return ""

    def _calc_win_rate(self, trades: list[dict], dim: str, val: str) -> float:
        matching = [t for t in trades if self._extract_dimension_value(t, dim) == val]
        if not matching:
            return 0
        wins = sum(1 for t in matching if t.get("result") == "win")
        return round(wins / len(matching) * 100, 1)

    def _count_trades(self, trades: list[dict], dim: str, val: str) -> int:
        return sum(1 for t in trades if self._extract_dimension_value(t, dim) == val)

    @staticmethod
    def _compute_modifier(wr_7d: float, wr_30d: float, sample: int) -> float:
        """Reponse graduee basee principalement sur le WR 7 jours."""
        if sample < MIN_TRADES_FOR_MODIFIER:
            return 0

        # Utiliser principalement le WR 7d pour etre reactif
        wr = wr_7d if wr_7d > 0 else wr_30d

        if wr < 30 and sample >= 8:
            return -15  # Penalite forte
        elif wr < 40:
            return -8   # Malus
        elif wr > 65:
            return 5    # Bonus
        return 0

    async def get_edge_decay_alerts(self) -> list[dict]:
        """Detecte les dimensions dont le WR 7j chute vs WR 30j (edge decay)."""
        await self._ensure_cache()
        alerts = []
        for key, w in self._cache.items():
            wr_7d = w.get("win_rate_7d", 0)
            wr_30d = w.get("win_rate_30d", 0)
            sample = w.get("sample_size", 0)
            if sample >= MIN_TRADES_FOR_MODIFIER and wr_30d > 0 and (wr_30d - wr_7d) >= 15:
                alerts.append({
                    "dimension": w["dimension"],
                    "value": w["dimension_value"],
                    "wr_7d": wr_7d,
                    "wr_30d": wr_30d,
                    "drop": round(wr_30d - wr_7d, 1),
                    "sample_size": sample,
                })
        return alerts

    async def get_calibration(self) -> list[dict]:
        """Win rate reel par tranche de score (calibration)."""
        await self._ensure_cache()
        result = []
        for rng in ["50-59", "60-69", "70-79", "80+"]:
            key = f"score_range:{rng}"
            w = self._cache.get(key, {})
            result.append({
                "score_range": rng,
                "win_rate_7d": w.get("win_rate_7d", 0),
                "win_rate_30d": w.get("win_rate_30d", 0),
                "win_rate_all": w.get("win_rate_all", 0),
                "sample_size": w.get("sample_size", 0),
                "avg_pnl": w.get("avg_pnl", 0),
            })
        return result
