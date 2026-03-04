"""
Microstructure Analysis : VPIN, Sweep Detection, Tape Speed.
Toutes les metriques sont calculees a partir des tick data existants (OrderFlowTracker).
Zero API supplementaire.
V4 only.
"""
import logging
import math
from collections import deque
from datetime import datetime

logger = logging.getLogger(__name__)


class MicrostructureAnalyzer:
    """
    Analyse la microstructure du marche a partir des ticks bruts.
    Connecte a un OrderFlowTracker pour lire ses _trades.
    """

    def __init__(self, order_flow_tracker):
        self.oft = order_flow_tracker
        # VPIN volume bucket cache per symbol
        self._vpin_cache: dict[str, dict] = {}

    # =========================================================
    # VPIN — Volume-Synchronized Probability of Informed Trading
    # =========================================================

    def compute_vpin(self, symbol: str, bucket_size: float = 0, num_buckets: int = 50) -> dict:
        """
        Compute VPIN from tick data using volume buckets.
        bucket_size: volume per bucket (auto-calculated if 0).
        num_buckets: number of completed buckets to use.

        Returns:
            vpin: 0.0-1.0 (probability of informed trading)
            bias: "buy" or "sell" (which side the informed flow is on)
            bucket_count: how many buckets were computed
        """
        trades = self.oft._trades.get(symbol, deque())
        if len(trades) < 50:
            return {"vpin": 0.5, "bias": "neutral", "bucket_count": 0, "confidence": 0}

        # Auto-calculate bucket size: total volume / (num_buckets * 2) for ~100 buckets worth
        now = datetime.utcnow().timestamp()
        cutoff = now - 900  # 15 minutes of data
        recent = [(ts, vol, is_buy, price) for ts, vol, is_buy, price in trades if ts >= cutoff]

        if len(recent) < 30:
            return {"vpin": 0.5, "bias": "neutral", "bucket_count": 0, "confidence": 0}

        total_vol = sum(vol for _, vol, _, _ in recent)
        if total_vol <= 0:
            return {"vpin": 0.5, "bias": "neutral", "bucket_count": 0, "confidence": 0}

        if bucket_size <= 0:
            bucket_size = total_vol / max(num_buckets * 2, 1)
            if bucket_size <= 0:
                return {"vpin": 0.5, "bias": "neutral", "bucket_count": 0, "confidence": 0}

        # Build volume buckets
        buckets = []
        current_buy = 0.0
        current_sell = 0.0
        current_vol = 0.0

        for ts, vol, is_buy, price in recent:
            remaining = vol
            while remaining > 0:
                space = bucket_size - current_vol
                fill = min(remaining, space)

                if is_buy:
                    current_buy += fill
                else:
                    current_sell += fill
                current_vol += fill
                remaining -= fill

                if current_vol >= bucket_size - 1e-10:
                    buckets.append((current_buy, current_sell))
                    current_buy = 0.0
                    current_sell = 0.0
                    current_vol = 0.0

        if len(buckets) < 5:
            return {"vpin": 0.5, "bias": "neutral", "bucket_count": len(buckets), "confidence": 0}

        # VPIN = average |buy_vol - sell_vol| / bucket_size over last N buckets
        use_buckets = buckets[-min(num_buckets, len(buckets)):]
        order_imbalances = [abs(b - s) / max(b + s, 1e-8) for b, s in use_buckets]
        vpin = sum(order_imbalances) / len(order_imbalances)

        # Determine bias: net direction of informed flow
        net_buy = sum(b for b, s in use_buckets)
        net_sell = sum(s for b, s in use_buckets)
        bias = "buy" if net_buy > net_sell else "sell" if net_sell > net_buy else "neutral"

        # Confidence based on bucket count
        confidence = min(1.0, len(use_buckets) / 30)

        return {
            "vpin": round(vpin, 4),
            "bias": bias,
            "bucket_count": len(use_buckets),
            "confidence": round(confidence, 2),
            "net_buy_pct": round(net_buy / max(net_buy + net_sell, 1e-8), 3),
        }

    # =========================================================
    # SWEEP DETECTION
    # =========================================================

    def detect_sweeps(self, symbol: str, window: int = 30, min_trades: int = 3,
                      time_window_ms: float = 2.0, vol_multiplier: float = 5.0) -> dict:
        """
        Detect price sweeps: rapid consecutive trades eating through price levels.
        A sweep = min_trades trades in < time_window_ms seconds, all same direction,
        cumulative volume > vol_multiplier x median.

        Returns:
            sweep_detected: bool
            sweep_direction: "buy" / "sell" / "none"
            sweep_volume: total volume of sweep
            sweep_levels: number of price levels consumed
            sweep_intensity: 0-1 (how aggressive the sweep is)
        """
        now = datetime.utcnow().timestamp()
        cutoff = now - window
        trades = self.oft._trades.get(symbol, deque())

        recent = [(ts, vol, is_buy, price) for ts, vol, is_buy, price in trades if ts >= cutoff]
        if len(recent) < min_trades + 5:
            return {"sweep_detected": False, "sweep_direction": "none",
                    "sweep_volume": 0, "sweep_levels": 0, "sweep_intensity": 0}

        import statistics
        volumes = [vol for _, vol, _, _ in recent]
        median_vol = statistics.median(volumes) if volumes else 0
        threshold = median_vol * vol_multiplier

        # Scan for sweeps in the last `window` seconds
        best_sweep = None
        best_vol = 0

        for i in range(len(recent) - min_trades + 1):
            # Check if next min_trades trades form a sweep
            window_trades = recent[i:i + min_trades + 5]  # look ahead a bit
            first_ts = window_trades[0][0]

            # Collect consecutive same-direction trades within time window
            direction = window_trades[0][2]  # is_buy
            sweep_trades = []
            prices = set()

            for ts, vol, is_buy, price in window_trades:
                if ts - first_ts > time_window_ms:
                    break
                if is_buy != direction:
                    break
                sweep_trades.append((ts, vol, is_buy, price))
                prices.add(price)

            if len(sweep_trades) >= min_trades:
                total_vol = sum(v for _, v, _, _ in sweep_trades)
                if total_vol >= threshold:
                    if total_vol > best_vol:
                        best_vol = total_vol
                        best_sweep = {
                            "direction": "buy" if direction else "sell",
                            "volume": total_vol,
                            "levels": len(prices),
                            "trade_count": len(sweep_trades),
                            "duration": sweep_trades[-1][0] - sweep_trades[0][0],
                        }

        if best_sweep:
            max_possible = median_vol * vol_multiplier * 10
            intensity = min(1.0, best_sweep["volume"] / max(max_possible, 1e-8))
            return {
                "sweep_detected": True,
                "sweep_direction": best_sweep["direction"],
                "sweep_volume": round(best_sweep["volume"], 2),
                "sweep_levels": best_sweep["levels"],
                "sweep_intensity": round(intensity, 3),
                "sweep_trades": best_sweep["trade_count"],
            }

        return {"sweep_detected": False, "sweep_direction": "none",
                "sweep_volume": 0, "sweep_levels": 0, "sweep_intensity": 0}

    # =========================================================
    # TAPE SPEED / TRADE INTENSITY
    # =========================================================

    def get_tape_speed(self, symbol: str) -> dict:
        """
        Measure trades per second (TPS) and detect acceleration.
        Compares current 30s TPS vs 5min average TPS.

        Returns:
            tps_current: trades per second (last 30s)
            tps_avg: trades per second (last 5min)
            acceleration: tps_current / tps_avg ratio
            intensity: "high" / "normal" / "low"
        """
        now = datetime.utcnow().timestamp()
        trades = self.oft._trades.get(symbol, deque())

        # Current 30s
        cutoff_30 = now - 30
        count_30 = sum(1 for ts, _, _, _ in trades if ts >= cutoff_30)
        tps_30 = count_30 / 30.0

        # 5min average
        cutoff_300 = now - 300
        count_300 = sum(1 for ts, _, _, _ in trades if ts >= cutoff_300)
        tps_300 = count_300 / 300.0

        acceleration = tps_30 / max(tps_300, 0.01)

        if acceleration > 3.0:
            intensity = "high"
        elif acceleration < 0.3:
            intensity = "low"
        else:
            intensity = "normal"

        return {
            "tps_current": round(tps_30, 2),
            "tps_avg": round(tps_300, 2),
            "acceleration": round(acceleration, 2),
            "intensity": intensity,
            "count_30s": count_30,
            "count_5m": count_300,
        }

    # =========================================================
    # TRADE IMBALANCE INTENSITY
    # =========================================================

    def get_trade_imbalance(self, symbol: str, window: int = 30) -> dict:
        """
        Measure consecutive trade imbalance (runs of same direction).
        Long runs of buys or sells indicate institutional accumulation.
        """
        now = datetime.utcnow().timestamp()
        cutoff = now - window
        trades = self.oft._trades.get(symbol, deque())

        recent = [(is_buy, vol) for ts, vol, is_buy, _ in trades if ts >= cutoff]
        if len(recent) < 5:
            return {"max_run": 0, "run_direction": "none", "imbalance": 0, "run_volume": 0}

        # Find longest consecutive run
        max_run = 0
        max_run_dir = None
        max_run_vol = 0
        current_run = 1
        current_dir = recent[0][0]
        current_vol = recent[0][1]

        for i in range(1, len(recent)):
            if recent[i][0] == current_dir:
                current_run += 1
                current_vol += recent[i][1]
            else:
                if current_run > max_run:
                    max_run = current_run
                    max_run_dir = current_dir
                    max_run_vol = current_vol
                current_run = 1
                current_dir = recent[i][0]
                current_vol = recent[i][1]

        if current_run > max_run:
            max_run = current_run
            max_run_dir = current_dir
            max_run_vol = current_vol

        # Overall imbalance
        buy_count = sum(1 for is_buy, _ in recent if is_buy)
        sell_count = len(recent) - buy_count
        total = len(recent)
        imbalance = (buy_count - sell_count) / max(total, 1)

        return {
            "max_run": max_run,
            "run_direction": "buy" if max_run_dir else "sell" if max_run_dir is not None else "none",
            "run_volume": round(max_run_vol, 2),
            "imbalance": round(imbalance, 3),
            "buy_pct": round(buy_count / max(total, 1) * 100, 1),
        }

    # =========================================================
    # COMBINED MICROSTRUCTURE REPORT
    # =========================================================

    def get_full_report(self, symbol: str) -> dict:
        """Get all microstructure metrics for a symbol."""
        last_ts = self.oft.get_last_trade_ts(symbol)
        now = datetime.utcnow().timestamp()
        is_stale = (now - last_ts) > 60 if last_ts > 0 else True

        if is_stale:
            return {
                "vpin": {"vpin": 0.5, "bias": "neutral", "bucket_count": 0, "confidence": 0},
                "sweep": {"sweep_detected": False, "sweep_direction": "none",
                          "sweep_volume": 0, "sweep_levels": 0, "sweep_intensity": 0},
                "tape_speed": {"tps_current": 0, "tps_avg": 0, "acceleration": 1.0,
                               "intensity": "low", "count_30s": 0, "count_5m": 0},
                "imbalance": {"max_run": 0, "run_direction": "none", "imbalance": 0, "run_volume": 0},
                "is_stale": True,
            }

        return {
            "vpin": self.compute_vpin(symbol),
            "sweep": self.detect_sweeps(symbol),
            "tape_speed": self.get_tape_speed(symbol),
            "imbalance": self.get_trade_imbalance(symbol),
            "is_stale": False,
        }
