"""
Service Sentiment : collecte et analyse le sentiment du marche crypto.
Sources gratuites : Fear & Greed Index, CryptoPanic news, BTC Dominance, Finnhub macro.
Cache 5 minutes pour eviter les appels excessifs.
"""
import logging
import time
import httpx
from app.config import CRYPTOPANIC_TOKEN, FINNHUB_TOKEN

logger = logging.getLogger(__name__)

CACHE_TTL = 300  # 5 minutes


class SentimentAnalyzer:
    def __init__(self):
        self._cache = None
        self._cache_time = 0

    async def get_sentiment(self) -> dict:
        """Retourne le sentiment global du marche crypto."""
        now = time.time()
        if self._cache and (now - self._cache_time) < CACHE_TTL:
            return self._cache

        score = 0
        reasons = []

        # 1. Fear & Greed Index
        fng = await self._fetch_fear_greed()
        fng_score, fng_reason = self._score_fear_greed(fng)
        score += fng_score
        if fng_reason:
            reasons.append(fng_reason)

        # 2. News sentiment (CryptoPanic)
        news_score = 0
        if CRYPTOPANIC_TOKEN:
            news_score = await self._fetch_news_sentiment()
            score += news_score
            if abs(news_score) >= 10:
                label = "bullish" if news_score > 0 else "bearish"
                reasons.append(f"News {label} ({news_score:+.0f})")

        # 3. BTC Dominance
        btc_dom, btc_dom_change = await self._fetch_btc_dominance()
        dom_score, dom_reason = self._score_btc_dominance(btc_dom, btc_dom_change)
        score += dom_score
        if dom_reason:
            reasons.append(dom_reason)

        # 4. Macro events
        macro_risk = "low"
        if FINNHUB_TOKEN:
            macro_risk = await self._fetch_macro_risk()
            if macro_risk == "high":
                score -= 10
                reasons.append("Event macro high-impact imminent")
            elif macro_risk == "medium":
                score -= 5
                reasons.append("Event macro medium-impact")

        # Clamp score entre -100 et +100
        score = max(-100, min(100, score))

        # Determiner le biais
        if score >= 20:
            bias = "bullish"
        elif score <= -20:
            bias = "bearish"
        else:
            bias = "neutral"

        result = {
            "score": score,
            "bias": bias,
            "fear_greed": fng,
            "news_score": news_score,
            "btc_dominance": btc_dom,
            "macro_risk": macro_risk,
            "reasons": reasons,
        }

        self._cache = result
        self._cache_time = now
        return result

    # --- Fear & Greed Index ---
    async def _fetch_fear_greed(self) -> int:
        """Fetch Fear & Greed Index (0-100). Gratuit, pas de cle."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    "https://api.alternative.me/fng/?limit=1",
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return int(data["data"][0]["value"])
        except Exception as e:
            logger.warning(f"Fear & Greed fetch erreur: {e}")
        return 50  # Neutre par defaut

    def _score_fear_greed(self, fng: int) -> tuple[int, str]:
        if fng <= 15:
            return -35, f"Fear & Greed {fng} (Extreme Fear)"
        elif fng <= 25:
            return -25, f"Fear & Greed {fng} (Extreme Fear)"
        elif fng <= 40:
            return -12, f"Fear & Greed {fng} (Fear)"
        elif fng <= 60:
            return 0, f"Fear & Greed {fng} (Neutral)"
        elif fng <= 75:
            return 12, f"Fear & Greed {fng} (Greed)"
        elif fng <= 85:
            return 25, f"Fear & Greed {fng} (Extreme Greed)"
        else:
            # Extreme greed = risque de reversal, un peu moins bullish
            return 20, f"Fear & Greed {fng} (Extreme Greed - reversal?)"

    # --- CryptoPanic News ---
    async def _fetch_news_sentiment(self) -> float:
        """Analyse les votes bullish/bearish des 20 derniers articles."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    "https://cryptopanic.com/api/posts/",
                    params={
                        "auth_token": CRYPTOPANIC_TOKEN,
                        "public": "true",
                        "filter": "important",
                        "kind": "news",
                    },
                    timeout=10,
                )
                if resp.status_code != 200:
                    return 0

                data = resp.json()
                posts = data.get("results", [])[:20]

                if not posts:
                    return 0

                bullish = 0
                bearish = 0
                for post in posts:
                    votes = post.get("votes", {})
                    bullish += votes.get("positive", 0) + votes.get("liked", 0)
                    bearish += votes.get("negative", 0) + votes.get("disliked", 0)

                total = bullish + bearish
                if total == 0:
                    return 0

                # Score de -50 a +50
                ratio = (bullish - bearish) / total
                return round(ratio * 50, 1)

        except Exception as e:
            logger.warning(f"CryptoPanic fetch erreur: {e}")
        return 0

    # --- BTC Dominance ---
    async def _fetch_btc_dominance(self) -> tuple[float, float]:
        """Retourne (btc_dominance_pct, variation estimee)."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    "https://api.coingecko.com/api/v3/global",
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    btc_dom = data["data"]["market_cap_percentage"].get("btc", 50)
                    # Variation approximative (pas de donnees historiques dans cet endpoint)
                    change = data["data"].get("market_cap_change_percentage_24h_usd", 0)
                    return round(btc_dom, 2), round(change, 2)
        except Exception as e:
            logger.warning(f"BTC Dominance fetch erreur: {e}")
        return 50.0, 0.0

    def _score_btc_dominance(self, btc_dom: float, market_change: float) -> tuple[int, str]:
        score = 0
        reason = ""

        # BTC dominance haute + marche en baisse = risk-off (mauvais pour altcoins)
        if btc_dom > 55 and market_change < -2:
            score = -15
            reason = f"BTC.D {btc_dom}% haute + marche baisse (risk-off)"
        elif btc_dom > 55:
            score = -5
            reason = f"BTC.D {btc_dom}% haute (altcoins sous pression)"
        elif btc_dom < 42 and market_change > 2:
            score = 10
            reason = f"BTC.D {btc_dom}% basse + marche hausse (alt season)"
        elif btc_dom < 45:
            score = 5
            reason = f"BTC.D {btc_dom}% (altcoins favorises)"

        return score, reason

    # --- Macro Events (Finnhub) ---
    async def _fetch_macro_risk(self) -> str:
        """Verifie les events macro high-impact dans les prochaines heures."""
        try:
            from datetime import datetime, timedelta
            today = datetime.utcnow().strftime("%Y-%m-%d")
            tomorrow = (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")

            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    "https://finnhub.io/api/v1/calendar/economic",
                    params={
                        "token": FINNHUB_TOKEN,
                        "from": today,
                        "to": tomorrow,
                    },
                    timeout=10,
                )
                if resp.status_code != 200:
                    return "low"

                data = resp.json()
                events = data.get("economicCalendar", [])

                # Mots cles high-impact
                high_impact_keywords = [
                    "interest rate", "fed", "fomc", "cpi", "inflation",
                    "nonfarm", "employment", "gdp", "pce",
                ]

                for event in events:
                    impact = event.get("impact", "").lower()
                    event_name = event.get("event", "").lower()

                    if impact == "high" or any(kw in event_name for kw in high_impact_keywords):
                        return "high"
                    if impact == "medium":
                        return "medium"

        except Exception as e:
            logger.warning(f"Finnhub macro fetch erreur: {e}")
        return "low"

    def invalidate_cache(self):
        """Force le refresh au prochain appel."""
        self._cache = None
        self._cache_time = 0


# Singleton
sentiment_analyzer = SentimentAnalyzer()
