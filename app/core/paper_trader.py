"""
Paper Trader : Execute automatiquement chaque signal en simulation.
Portefeuille fictif avec suivi P&L en temps reel via les vrais prix MEXC.
Accepte bot_version pour supporter V1 et V2 en parallele.
"""
import logging
from datetime import datetime, timedelta
from app.config import SETTINGS
from app.database import (
    get_paper_portfolio,
    reserve_paper_margin,
    update_paper_balance,
    init_paper_portfolio,
    get_trades,
)

logger = logging.getLogger(__name__)

FIXED_MARGIN = 10.0  # 10$ fixe par trade
MAX_OPEN = 5         # Max 5 positions simultanees


class PaperTrader:
    def __init__(self, bot_version="V2", settings=None):
        self.bot_version = bot_version
        self.settings = settings or SETTINGS
        self._open_positions: dict[int, float] = {}  # pos_id -> margin
        self._position_monitor = None  # set later
        self._correlation_guard = None  # V4 only
        self._circuit_breaker_until = None  # V4 only: pause until datetime

    def set_position_monitor(self, pm):
        self._position_monitor = pm

    def set_correlation_guard(self, cg):
        self._correlation_guard = cg

    async def start(self):
        """Initialise le portefeuille paper et enregistre le callback."""
        await init_paper_portfolio(100.0, self.bot_version)
        if self._position_monitor:
            self._position_monitor.add_on_close_callback(self._on_position_closed)
        portfolio = await get_paper_portfolio(self.bot_version)
        logger.info(
            f"PaperTrader [{self.bot_version}] demarre - Balance: ${portfolio['current_balance']:.2f} "
            f"({portfolio['total_trades']} trades, P&L: ${portfolio['total_pnl']:.2f})"
        )

    async def auto_execute(self, signal: dict) -> bool:
        """Execute automatiquement un signal en paper trading."""
        if signal.get("type") != "signal" or signal.get("direction") == "none":
            return False

        # V4: Circuit breaker check
        if self.bot_version == "V4":
            breaker_reason = await self._check_circuit_breaker()
            if breaker_reason:
                logger.info(f"[{self.bot_version}] Circuit breaker: {breaker_reason}, skip {signal['symbol']}")
                return False

        # V4: Dynamic max positions based on balance
        if self.bot_version == "V4":
            portfolio = await get_paper_portfolio(self.bot_version)
            sizing_cfg = self.settings.get("sizing", {})
            base_pct = sizing_cfg.get("base_pct", 8) / 100
            avg_margin = portfolio["current_balance"] * base_pct
            avg_margin = max(avg_margin, sizing_cfg.get("min_margin", 3))
            max_pos = max(2, min(6, int(portfolio["current_balance"] * 0.50 / avg_margin)))
        else:
            max_pos = MAX_OPEN

        if len(self._open_positions) >= max_pos:
            logger.debug(f"[{self.bot_version}] Paper: max positions ({max_pos}) atteint, skip {signal['symbol']}")
            return False

        # Verifier qu'on n'a pas deja une position sur ce symbol/direction
        if self._position_monitor:
            for pos_id, _ in self._open_positions.items():
                cached = self._position_monitor._positions.get(pos_id, {})
                if cached.get("symbol") == signal["symbol"] and cached.get("direction") == signal["direction"]:
                    logger.debug(f"[{self.bot_version}] Paper: deja une position {signal['symbol']} {signal['direction']}")
                    return False

        # V4 only: Anti-correlation guard: max 3 positions dans la meme direction
        if self.bot_version == "V4" and self._position_monitor:
            same_dir_count = sum(
                1 for p in self._position_monitor._positions.values()
                if p.get("state") != "closed" and p.get("direction") == signal["direction"]
            )
            if same_dir_count >= 3:
                logger.info(
                    f"[{self.bot_version}] Anti-correlation: {same_dir_count} {signal['direction'].upper()} "
                    f"deja ouvertes, skip {signal['symbol']}"
                )
                return False

            # V4: Cluster-based correlation guard
            if self._correlation_guard:
                active_positions = list(self._position_monitor._positions.values())
                allowed, reason = self._correlation_guard.check_correlation_limit(
                    signal["symbol"], signal["direction"], active_positions
                )
                if not allowed:
                    logger.info(f"[{self.bot_version}] {reason}")
                    return False

        # V4: Fee gate — skip if TP1 distance % < round-trip fees
        if self.bot_version == "V4":
            entry_price = signal.get("entry_price", 0)
            tp1 = signal.get("tp1", 0)
            if entry_price > 0 and tp1 != 0:
                tp1_dist_pct = abs(tp1 - entry_price) / entry_price * 100
                taker_pct = self.settings.get("fees", {}).get("taker_pct", 0.06)
                fees_rt_pct = taker_pct * 2  # round-trip fees in %
                if tp1_dist_pct < fees_rt_pct:
                    logger.info(
                        f"[{self.bot_version}] FEE GATE: skip {signal['symbol']} {signal.get('mode','')} "
                        f"TP1={tp1_dist_pct:.4f}% < fees {fees_rt_pct:.4f}%"
                    )
                    return False

        portfolio = await get_paper_portfolio(self.bot_version)
        available = portfolio["current_balance"] - portfolio["reserved_margin"]

        # V4: Dynamic position sizing based on score and balance
        if self.bot_version == "V4":
            sizing_cfg = self.settings.get("sizing", {})
            base_pct = sizing_cfg.get("base_pct", 8) / 100
            min_margin = sizing_cfg.get("min_margin", 3)
            max_margin = sizing_cfg.get("max_margin", 20)
            score = signal.get("score", 60)
            # Score scaling: 50→0.6x, 65→1.0x, 85→1.5x
            score_mult = 0.6 + (score - 50) * (0.9 / 35) if score <= 85 else 1.5
            score_mult = max(0.6, min(1.5, score_mult))
            margin = portfolio["current_balance"] * base_pct * score_mult
            margin = max(min_margin, min(max_margin, round(margin, 2)))
        else:
            margin = FIXED_MARGIN

        if margin > available:
            logger.warning(f"[{self.bot_version}] Paper: solde insuffisant ({available:.2f}$ dispo, {margin:.2f}$ requis)")
            return False

        leverage = signal.get("leverage", 10)
        entry_price = signal["entry_price"]

        # V4: Simulate slippage (half-spread, capped at 0.5% max)
        if self.bot_version == "V4":
            spread_pct = signal.get("_indicator_snapshot", {}).get("spread_pct", 0)
            if 0 < spread_pct <= 0.5:  # ignore absurd values (999 = missing data)
                half_spread = entry_price * (spread_pct / 100) / 2
                if signal["direction"] == "long":
                    entry_price += half_spread  # worse fill for long
                else:
                    entry_price -= half_spread  # worse fill for short
                entry_price = round(entry_price, 8)

        position_size_usd = margin * leverage
        quantity = round(position_size_usd / entry_price, 6)

        if quantity <= 0:
            return False

        fake_result = {
            "success": True,
            "order_type": "market",
            "entry_order_id": None,
            "actual_entry_price": entry_price,
            "sl_order_id": None,
            "tp_order_ids": [None, None, None],
            "quantity": quantity,
            "position_size_usd": round(position_size_usd, 2),
            "margin_required": round(margin, 2),
            "balance": round(available - margin, 2),
        }

        # Ajouter bot_version au signal pour insert_active_position
        signal["bot_version"] = self.bot_version

        if not self._position_monitor:
            return False

        pos_id = await self._position_monitor.register_trade(signal, fake_result)
        if pos_id is None:
            return False

        await reserve_paper_margin(margin, self.bot_version)
        self._open_positions[pos_id] = margin

        logger.info(
            f"[{self.bot_version}] PAPER TRADE: {signal['direction'].upper()} {signal['symbol']} "
            f"qty={quantity} marge={margin:.2f}$ pos={position_size_usd:.2f}$ "
            f"(balance: {available - margin:.2f}$)"
        )
        return True

    async def _check_circuit_breaker(self) -> str | None:
        """V4: Check if trading should be paused due to losses."""
        # Check if we're in a pause period
        if self._circuit_breaker_until and datetime.utcnow() < self._circuit_breaker_until:
            remaining = (self._circuit_breaker_until - datetime.utcnow()).total_seconds() / 60
            return f"pause active ({remaining:.0f}min restantes)"

        self._circuit_breaker_until = None

        risk_limits = self.settings.get("risk_limits", {})
        max_daily_loss = risk_limits.get("max_daily_loss_usd", 0)
        max_consecutive = risk_limits.get("max_consecutive_losses", 0)
        pause_minutes = risk_limits.get("pause_minutes", 60)

        if max_daily_loss <= 0 and max_consecutive <= 0:
            return None

        # Get recent trades
        recent_trades = await get_trades(limit=20, bot_version=self.bot_version)
        if not recent_trades:
            return None

        # Check daily loss
        if max_daily_loss > 0:
            daily_loss = 0
            cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()
            for t in recent_trades:
                if t.get("exit_time", "") >= cutoff:
                    daily_loss += t.get("pnl_usd", 0)
            if daily_loss <= -max_daily_loss:
                self._circuit_breaker_until = datetime.utcnow() + timedelta(minutes=pause_minutes)
                return f"daily loss ${daily_loss:.2f} >= -${max_daily_loss}"

        # Check consecutive losses
        if max_consecutive > 0:
            consecutive = 0
            for t in recent_trades:
                if t.get("result") == "loss":
                    consecutive += 1
                else:
                    break
            if consecutive >= max_consecutive:
                self._circuit_breaker_until = datetime.utcnow() + timedelta(minutes=pause_minutes)
                return f"{consecutive} consecutive losses >= {max_consecutive}"

        return None

    async def _on_position_closed(self, pos_id: int, pnl_usd: float):
        """Callback quand le position_monitor ferme une position."""
        margin = self._open_positions.pop(pos_id, None)
        if margin is None:
            return

        is_win = pnl_usd > 0
        await update_paper_balance(pnl_usd, is_win, margin, self.bot_version)

        portfolio = await get_paper_portfolio(self.bot_version)
        sign = "+" if pnl_usd >= 0 else ""
        logger.info(
            f"[{self.bot_version}] PAPER CLOSE: PnL {sign}{pnl_usd:.2f}$ | "
            f"Balance: ${portfolio['current_balance']:.2f} | "
            f"Win/Loss: {portfolio['wins']}/{portfolio['losses']}"
        )
