# CryptoSignals - Etat du Projet

## Vue d'ensemble
Bot de trading crypto MEXC Futures avec dashboard web. Dual bot (V1 strict + V2 loose) en paper trading.

- **URL** : https://crypto.swipego.app
- **Coolify UUID** : `mwk444s084kgkcsg8ko80wco`
- **Repo** : https://github.com/AmazingeventParis/CryptoSignals

## Stack
- Python 3.12 + FastAPI + uvicorn
- ccxt 4.5.38 (MEXC Futures)
- SQLite (aiosqlite)
- Frontend: HTML/CSS/JS vanilla + TradingView charts
- Docker sur Coolify

## Architecture - Dual Bot
- **V1** : Score min 65/70, spread kill 0.15%, conservateur
- **V2** : Score min 45/50, spread kill 0.30%, plus de signaux
- Chaque bot a son portfolio paper $100, $10/trade, max 5 positions

## Moteur de Signal (4 couches)
1. **Tradeability (30%)** : Volatilite, volume, spread, depth, funding, OI
2. **Direction (25%)** : EMA cross, structure marche, RSI
3. **Entry (25%)** : Breakout, retest, divergence RSI, EMA bounce
4. **Sentiment (20%)** : Fear & Greed, news, BTC dominance

## Structure
```
app/
  main.py          - FastAPI entry, dual bot
  config.py        - Load settings V1+V2
  database.py      - SQLite schema + CRUD (7 tables)
  core/
    scanner.py     - Boucle 30s, analyse 9 paires x 2 modes
    signal_engine.py - Score 0-100 (4 couches)
    paper_trader.py  - Trading simule
    position_monitor.py - Suivi temps reel WebSocket
    risk_manager.py  - SL, TP1/2/3, leverage
    indicators.py    - EMA, RSI, ATR, Bollinger, VWAP
    tradeability.py  - Layer A: 6 checks + kill switches
    direction.py     - Layer B: EMA cross + structure + RSI
    entry.py         - Layer C: 4 setups
    trade_learner.py - Desactive combos perdants
  api/routes.py    - 30+ endpoints REST
  services/
    sentiment.py   - Fear & Greed, news, macro
    telegram_bot.py - Push notifications (desactive)
  static/          - Dashboard (7 onglets)
config/
  settings_V1.yaml - Config stricte
  settings_V2.yaml - Config loose
data/signals.db    - Base SQLite
```

## Dashboard (7 onglets)
1. Bot V1 - Positions + signaux
2. Bot V2 - Positions + signaux
3. Freqtrade - Proxy trades externes
4. Charts - TradingView avec WebSocket relay
5. Journal - Tous les trades
6. VS Comparaison - Courbes P&L superposees (V1=bleu, V2=violet, FT=orange)
7. Pairs - 9 paires monitorees

## Paires monitorees (9)
XRP, DOGE, PEPE, RUNE, SOL, KAITO, BTC, VIRTUAL, TRUMP (tous /USDT:USDT)

## DB (7 tables)
- signals, trades_journal, active_positions, paper_portfolio
- tradeability_log, market_snapshots, setup_performance
- Colonne `bot_version` (V1/V2) sur tables principales

## API principales
- `GET /api/signals?bot_version=V1` - Signaux recents
- `GET /api/positions/live?bot_version=V1` - Positions actives
- `GET /api/stats?bot_version=V1` - Win rate, P&L
- `GET /api/debug/{symbol}?mode=scalping&bot_version=V2` - Analyse complete
- `POST /api/positions/{id}/close` - Fermeture manuelle
- `GET /api/freqtrade/openTrades` - Proxy Freqtrade
- `POST /api/config/reload` - Hot-reload configs
- `WS /ws/kline/{symbol}/{timeframe}` - Candles temps reel

## Auth
- Password login (cookie signe itsdangerous, 30 jours)
- Env: DASHBOARD_PASSWORD, SESSION_SECRET

## Env vars
- MEXC_API_KEY/SECRET (optionnel, paper mode)
- DASHBOARD_PASSWORD, SESSION_SECRET
- APP_MODE=paper, LOG_LEVEL=INFO
- CRYPTOPANIC_TOKEN, FINNHUB_TOKEN (optionnels, sentiment)

## Problemes connus
- Chart zoom reset → utiliser update() pas setData()
- WebSocket deconnexion → fallback polling 30s
- Cache navigateur → incrementer ?v=N dans HTML (actuellement v=66)
- Windows git → "nul" file, utiliser git add explicite
- Service Worker desactive (cache stale)

## Deploy
```bash
git push origin main
curl -s -X GET "https://coolify.swipego.app/api/v1/deploy?uuid=mwk444s084kgkcsg8ko80wco&force=true" \
  -H "Authorization: Bearer 1|FNcssp3CipkrPNVSQyv3IboYwGsP8sjPskoBG3ux98e5a576"
```
