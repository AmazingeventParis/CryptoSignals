import ccxt.async_support as ccxt
import pandas as pd
import logging
from datetime import datetime, timedelta
from app.config import MEXC_API_KEY, MEXC_SECRET_KEY, get_enabled_pairs

logger = logging.getLogger(__name__)


class MarketData:
    def __init__(self):
        self.exchange: ccxt.mexc = None
        self.exchange_private: ccxt.mexc = None
        self._cache: dict = {}
        self._last_oi: dict[str, float] = {}  # symbol -> last OI value

    def is_connected(self) -> bool:
        return self.exchange is not None and self.exchange.markets is not None

    async def connect(self):
        # Exchange public (pas de cle API, pas de restriction IP)
        try:
            self.exchange = ccxt.mexc({
                "options": {"defaultType": "swap"},
                "enableRateLimit": True,
            })
            await self.exchange.load_markets()
            logger.info(f"Connecte a MEXC Futures (public) - {len(self.exchange.markets)} marches")
        except Exception as e:
            logger.warning(f"Connexion MEXC public echouee: {e}")
            if self.exchange:
                try:
                    await self.exchange.close()
                except Exception:
                    pass
                self.exchange = None
            return

        # Exchange prive (pour balance uniquement)
        if MEXC_API_KEY and MEXC_SECRET_KEY:
            try:
                self.exchange_private = ccxt.mexc({
                    "apiKey": MEXC_API_KEY,
                    "secret": MEXC_SECRET_KEY,
                    "options": {"defaultType": "swap"},
                    "enableRateLimit": True,
                })
                self.exchange_private.markets = self.exchange.markets
                logger.info("MEXC prive configure (pour balance)")
            except Exception as e:
                logger.warning(f"MEXC prive non disponible: {e}")
                self.exchange_private = None

    async def close(self):
        if self.exchange:
            await self.exchange.close()
        if self.exchange_private:
            await self.exchange_private.close()

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
            logger.error(f"Erreur fetch_orderbook {symbol}: {type(e).__name__}: {e}")
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
        if not self.exchange_private:
            return {"total": 0, "free": 0, "used": 0}
        try:
            balance = await self.exchange_private.fetch_balance()
            usdt = balance.get("USDT", {})
            return {
                "total": usdt.get("total", 0),
                "free": usdt.get("free", 0),
                "used": usdt.get("used", 0),
            }
        except Exception as e:
            logger.error(f"Erreur fetch_balance: {e}")
            return {"total": 0, "free": 0, "used": 0}

    def get_oi_change_pct(self, symbol: str, current_oi: float) -> float:
        """Calcule le % de changement de l'OI par rapport a la derniere valeur."""
        prev_oi = self._last_oi.get(symbol, 0)
        self._last_oi[symbol] = current_oi
        if prev_oi <= 0 or current_oi <= 0:
            return 0.0
        return ((current_oi - prev_oi) / prev_oi) * 100

    async def fetch_all_data(self, symbol: str, timeframes: list[str]) -> dict:
        """Recupere toutes les donnees pour une paire et ses timeframes."""
        data = {
            "symbol": symbol,
            "timestamp": datetime.utcnow().isoformat(),
            "ohlcv": {},
            "orderbook": {},
            "funding_rate": 0.0,
            "open_interest": 0.0,
            "oi_change_pct": 0.0,
            "ticker": {},
        }

        for tf in timeframes:
            data["ohlcv"][tf] = await self.fetch_ohlcv(symbol, tf)

        data["orderbook"] = await self.fetch_orderbook(symbol)
        data["funding_rate"] = await self.fetch_funding_rate(symbol)
        data["open_interest"] = await self.fetch_open_interest(symbol)
        data["oi_change_pct"] = self.get_oi_change_pct(symbol, data["open_interest"])
        data["ticker"] = await self.fetch_ticker(symbol)

        return data

    async def fetch_all_data_batch(self, symbols: list[str], timeframes: list[str]) -> dict[str, dict]:
        """Recupere les donnees pour toutes les paires en parallele."""
        import asyncio
        tasks = {symbol: self.fetch_all_data(symbol, timeframes) for symbol in symbols}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        data = {}
        for symbol, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                logger.error(f"Erreur fetch_all_data_batch {symbol}: {result}")
                data[symbol] = None
            else:
                data[symbol] = result
        return data


# Singleton
market_data = MarketData()
