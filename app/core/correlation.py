"""
Correlation Guard : Matrice correlation roulante entre paires.
Bloque les positions sur des paires trop correlees (meme cluster + direction).
V4 only.
"""
import logging
import numpy as np
from collections import defaultdict

logger = logging.getLogger(__name__)

# Pre-defined clusters based on known crypto correlations
# These are updated dynamically if enough data is available
DEFAULT_CLUSTERS = {
    "btc_eco": ["BTC/USDT:USDT", "SOL/USDT:USDT"],
    "meme": ["DOGE/USDT:USDT", "PEPE/USDT:USDT", "WIF/USDT:USDT", "TRUMP/USDT:USDT"],
    "alt_l1": ["AVAX/USDT:USDT", "NEAR/USDT:USDT", "SUI/USDT:USDT"],
    "defi": ["LINK/USDT:USDT", "ARB/USDT:USDT"],
    "other": ["XRP/USDT:USDT", "RUNE/USDT:USDT", "KAITO/USDT:USDT", "VIRTUAL/USDT:USDT"],
}

MAX_CORRELATED_POSITIONS = 3


class CorrelationGuard:
    def __init__(self):
        self._price_history: dict[str, list[float]] = defaultdict(list)
        self._max_history = 60  # 60 samples (~30min at 30s intervals)
        self._clusters: dict[str, list[str]] = dict(DEFAULT_CLUSTERS)

    def update_price(self, symbol: str, price: float):
        """Add a price tick to history."""
        history = self._price_history[symbol]
        history.append(price)
        if len(history) > self._max_history:
            history.pop(0)

    def get_cluster(self, symbol: str) -> str:
        """Return the cluster name for a symbol."""
        for cluster_name, symbols in self._clusters.items():
            if symbol in symbols:
                return cluster_name
        return "unknown"

    def check_correlation_limit(self, symbol: str, direction: str, active_positions: list[dict]) -> tuple[bool, str]:
        """
        Check if opening a new position would exceed correlation limits.
        Returns (allowed, reason).
        """
        target_cluster = self.get_cluster(symbol)

        # Count positions in same cluster + same direction
        same_cluster_dir = 0
        for pos in active_positions:
            if pos.get("state") == "closed":
                continue
            pos_cluster = self.get_cluster(pos["symbol"])
            if pos_cluster == target_cluster and pos.get("direction") == direction:
                same_cluster_dir += 1

        if same_cluster_dir >= MAX_CORRELATED_POSITIONS:
            return False, (
                f"Correlation limit: {same_cluster_dir} {direction.upper()} "
                f"in cluster '{target_cluster}' (max {MAX_CORRELATED_POSITIONS})"
            )

        return True, ""

    def compute_correlation_matrix(self) -> dict:
        """Compute rolling correlation matrix from price returns."""
        symbols = [s for s, h in self._price_history.items() if len(h) >= 10]
        if len(symbols) < 2:
            return {}

        # Compute returns
        returns = {}
        for sym in symbols:
            prices = self._price_history[sym]
            ret = [(prices[i] - prices[i-1]) / prices[i-1] for i in range(1, len(prices))]
            returns[sym] = ret

        # Align lengths
        min_len = min(len(r) for r in returns.values())
        if min_len < 5:
            return {}

        matrix = {}
        for i, sym1 in enumerate(symbols):
            for sym2 in symbols[i+1:]:
                r1 = np.array(returns[sym1][-min_len:])
                r2 = np.array(returns[sym2][-min_len:])
                if np.std(r1) == 0 or np.std(r2) == 0:
                    continue
                corr = float(np.corrcoef(r1, r2)[0, 1])
                matrix[f"{sym1}|{sym2}"] = round(corr, 3)

        return matrix
