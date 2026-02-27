"""
Paper Trader : Execute automatiquement chaque signal en simulation.
Portefeuille fictif avec suivi P&L en temps reel via les vrais prix MEXC.
Accepte bot_version pour supporter V1 et V2 en parallele.
"""
import logging
from app.config import SETTINGS
from app.database import (
    get_paper_portfolio,
    reserve_paper_margin,
    update_paper_balance,
    init_paper_portfolio,
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

    def set_position_monitor(self, pm):
        self._position_monitor = pm

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

        if len(self._open_positions) >= MAX_OPEN:
            logger.debug(f"[{self.bot_version}] Paper: max positions ({MAX_OPEN}) atteint, skip {signal['symbol']}")
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

        portfolio = await get_paper_portfolio(self.bot_version)
        available = portfolio["current_balance"] - portfolio["reserved_margin"]
        margin = FIXED_MARGIN

        if margin > available:
            logger.warning(f"[{self.bot_version}] Paper: solde insuffisant ({available:.2f}$ dispo, {margin:.2f}$ requis)")
            return False

        leverage = signal.get("leverage", 10)
        entry_price = signal["entry_price"]
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
