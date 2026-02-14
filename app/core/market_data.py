import ccxt.async_support as ccxt
import pandas as pd
import logging
from datetime import datetime, timedelta
from app.config import MEXC_API_KEY, MEXC_SECRET_KEY, get_enabled_pairs

logger = logging.getLogger(__name__)


class MarketData:
    def __init__(self):
        self.exchange: ccxt.mexc = None
        self._cache: dict = {}

    def is_connected(self) -> bool:
        return self.exchange is not None and self.exchange.markets is not None

    async def connect(self):
        try:
            self.exchange = ccxt.mexc({
                "apiKey": MEXC_API_KEY,
                "secret": MEXC_SECRET_KEY,
                "options": {
                    "defaultType": "swap",
                    "fetchCurrencies": False,
                },
                "enableRateLimit": True,
            })
            await self.exchange.load_markets()
            logger.info(f"Connecte a MEXC Futures - {len(self.exchange.markets)} marches charges")
        except Exception as e:
            logger.warning(f"Connexion MEXC echouee (retry au prochain scan): {e}")
            if self.exchange:
                try:
                    await self.exchange.close()
                except Exception:
                    pass
                self.exchange = None

    async def close(self):
        if self.exchange:
            await self.exchange.close()

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 100) -> pd.DataFrame:
        try:
            ohlcv = await self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            df.set_index("timestamp", inplace=True)
            return df
        except Exception as e:
            logger.error(f"Erreur fetch_ohlcv {symbol} {timeframe}: {e}")
            return pd.DataFrame()

    async def fetch_orderbook(self, symbol: str, limit: int = 10) -> dict:
        try:
            ob = await self.exchange.fetch_order_book(symbol, limit=limit)
            if not ob["bids"] or not ob["asks"]:
                return {"spread_pct": 999, "bid_depth": 0, "ask_depth": 0, "mid_price": 0}

            best_bid = ob["bids"][0][0]
            best_ask = ob["asks"][0][0]
            mid_price = (best_bid + best_ask) / 2
            spread_pct = ((best_ask - best_bid) / mid_price) * 100

            bid_depth = sum(qty for _, qty in ob["bids"][:5])
            ask_depth = sum(qty for _, qty in ob["asks"][:5])

            return {
                "spread_pct": round(spread_pct, 6),
                "bid_depth": bid_depth,
                "ask_depth": ask_depth,
                "mid_price": mid_price,
                "best_bid": best_bid,
                "best_ask": best_ask,
            }
        except Exception as e:
            logger.error(f"Erreur fetch_orderbook {symbol}: {e}")
            return {"spread_pct": 999, "bid_depth": 0, "ask_depth": 0, "mid_price": 0}

    async def fetch_funding_rate(self, symbol: str) -> float:
        try:
            funding = await self.exchange.fetch_funding_rate(symbol)
            return funding.get("fundingRate", 0) * 100  # en %
        except Exception as e:
            logger.error(f"Erreur fetch_funding_rate {symbol}: {e}")
            return 0.0

    async def fetch_open_interest(self, symbol: str) -> float:
        try:
            oi = await self.exchange.fetch_open_interest(symbol)
            return oi.get("openInterestAmount", 0)
        except Exception as e:
            logger.error(f"Erreur fetch_open_interest {symbol}: {e}")
            return 0.0

    async def fetch_ticker(self, symbol: str) -> dict:
        try:
            ticker = await self.exchange.fetch_ticker(symbol)
            return {
                "price": ticker.get("last", 0),
                "volume_24h": ticker.get("quoteVolume", 0),
                "change_24h_pct": ticker.get("percentage", 0),
                "high_24h": ticker.get("high", 0),
                "low_24h": ticker.get("low", 0),
            }
        except Exception as e:
            logger.error(f"Erreur fetch_ticker {symbol}: {e}")
            return {"price": 0, "volume_24h": 0, "change_24h_pct": 0}

    async def fetch_balance(self) -> dict:
        try:
            balance = await self.exchange.fetch_balance()
            usdt = balance.get("USDT", {})
            return {
                "total": usdt.get("total", 0),
                "free": usdt.get("free", 0),
                "used": usdt.get("used", 0),
            }
        except Exception as e:
            logger.error(f"Erreur fetch_balance: {e}")
            return {"total": 0, "free": 0, "used": 0}

    async def fetch_all_data(self, symbol: str, timeframes: list[str]) -> dict:
        """Recupere toutes les donnees pour une paire et ses timeframes."""
        data = {
            "symbol": symbol,
            "timestamp": datetime.utcnow().isoformat(),
            "ohlcv": {},
            "orderbook": {},
            "funding_rate": 0.0,
            "open_interest": 0.0,
            "ticker": {},
        }

        for tf in timeframes:
            data["ohlcv"][tf] = await self.fetch_ohlcv(symbol, tf)

        data["orderbook"] = await self.fetch_orderbook(symbol)
        data["funding_rate"] = await self.fetch_funding_rate(symbol)
        data["open_interest"] = await self.fetch_open_interest(symbol)
        data["ticker"] = await self.fetch_ticker(symbol)

        return data


# Singleton
market_data = MarketData()
