# CryptoSignals - Reference Projet

## REGLE ABSOLUE
- **NE JAMAIS toucher aux trades/signaux/positions lors des modifications du site**
- Les donnees de trading doivent rester intactes pour avoir de vraies stats sur le temps
- Reset effectue le 2026-02-16 : V1 et V2 a $100, 0 trades

---

## Stack Technique
- **Backend** : Python 3.12 + FastAPI + uvicorn
- **Market Data** : ccxt 4.5.38 (MEXC Futures)
- **Indicateurs** : pandas, numpy, ta
- **Base de donnees** : SQLite (aiosqlite)
- **Notifications** : Telegram via httpx (desactive actuellement)
- **Frontend** : HTML/CSS/JS + TradingView lightweight-charts 4.1.3
- **Temps reel** : WebSocket relay serveur (MEXC -> FastAPI -> navigateur)
- **Deploiement** : Docker sur OVH via Coolify
- **Repo** : https://github.com/AmazingeventParis/CryptoSignals (public)

---

## Deploiement

### Coolify
- **URL** : https://crypto.swipego.app
- **App UUID** : `rww8go0ccggsswg44cokggco`
- **FQDN** : `http://crypto.swipego.app` (http, pas https - Nginx gere SSL)
- **Deploy** : `curl -s -X GET "https://coolify.swipego.app/api/v1/deploy?uuid=rww8go0ccggsswg44cokggco&force=true" -H "Authorization: Bearer 1|3zjGA1sbpBEOzOTFJUjWXtU4wCrF4KsL1cJ3ygzVe2970df0"`

### Workflow
```bash
git add <fichiers> && git commit -m "message" && git push origin master
# Puis deploy Coolify (curl ci-dessus)
# Attendre ~60-90s puis verifier
```

---

## Architecture Dual Bot (v2.0)

### Principe
Deux bots tournent en parallele pour comparer les performances :
- **V1 (strict)** : min_score scalp=65/swing=70, tradeability min 0.60, spread kill 0.15%
- **V2 (assoupli)** : min_score scalp=45/swing=50, tradeability min 0.35, spread kill 0.30%
- **Freqtrade** : bot externe (CombinedStrategy EMA9/21 + RSI + BB + ADX)

### Configs
- `config/settings_v1.yaml` — Config stricte
- `config/settings_v2.yaml` — Config assouplie
- `app/config.py` — Charge les deux : `SETTINGS_V1`, `SETTINGS_V2`, `SETTINGS = SETTINGS_V2`

### Instances
Plus de singletons. Tout est instancie dans `main.py` :
```python
bot_instances = {
    "V1": { scanner, paper_trader, position_monitor },
    "V2": { scanner, paper_trader, position_monitor },
}
```
- `routes.py` accede aux instances via `_get_bot_instances()`
- DB : colonne `bot_version` sur toutes les tables (migration auto)
- Chaque bot a son propre portfolio papier de $100

### Ressources partagees
- `market_data` (MEXC) = une seule connexion, les 2 bots lisent les memes prix
- `sentiment_analyzer` = partage

---

## Structure du Projet

```
Cypto/
├── .env                          # Secrets (pas commite)
├── Dockerfile                    # Python 3.12-slim, uvicorn port 8000
├── requirements.txt              # ccxt==4.5.38, fastapi, etc.
├── config/
│   ├── settings_v1.yaml          # Config V1 stricte
│   └── settings_v2.yaml          # Config V2 assouplie
├── data/
│   └── signals.db                # SQLite
└── app/
    ├── config.py                 # Charge settings V1+V2 + .env
    ├── database.py               # SQLite schema + CRUD + bot_version
    ├── main.py                   # FastAPI app + dual bot lifespan + WS relay + auth
    ├── core/
    │   ├── market_data.py        # Connexion MEXC (public + prive)
    │   ├── indicators.py         # EMA, RSI, ATR, BB, VWAP, structure, divergence
    │   ├── tradeability.py       # Layer A: 6 checks + kill switches
    │   ├── direction.py          # Layer B: EMA cross + structure + RSI
    │   ├── entry.py              # Layer C: breakout, retest, divergence, ema_bounce
    │   ├── signal_engine.py      # Combine A+B+C -> signal (accepte settings en param)
    │   ├── risk_manager.py       # SL, TP1/2/3, leverage, sizing
    │   ├── scanner.py            # Boucle 30s, accepte name+settings (plus de singleton)
    │   ├── paper_trader.py       # Paper trading auto (accepte bot_version)
    │   ├── position_monitor.py   # Suivi positions + trailing stop (accepte bot_version)
    │   ├── order_executor.py     # Ordres MEXC Futures (position_monitor en param)
    │   └── trade_learner.py      # Apprentissage: desactive combos perdants
    ├── services/
    │   ├── telegram_bot.py       # Notifications Telegram
    │   └── sentiment.py          # Analyse sentiment marche
    ├── api/
    │   └── routes.py             # REST endpoints + Freqtrade proxy
    └── static/
        ├── index.html            # Dashboard (v=66)
        ├── style.css             # Theme sombre (v=66)
        ├── app.js                # Frontend JS (v=66)
        └── login.html            # Page connexion
```

---

## Database (SQLite)

### Tables
| Table | Colonnes cles | bot_version |
|-------|--------------|-------------|
| `signals` | symbol, mode, direction, score, entry_price, SL, TP1-3, status | oui |
| `trades_journal` | entry/exit_price, pnl_usd, pnl_pct, result, entry/exit_time | oui |
| `active_positions` | symbol, direction, entry_price, SL, TP1-3, qty, state | oui |
| `paper_portfolio` | initial/current_balance, wins, losses, total_pnl | oui |
| `tradeability_log` | symbol, score, is_tradable, details | oui |
| `market_snapshots` | symbol, price, volume, spread, funding, atr, rsi | non |
| `setup_performance` | setup_type, symbol, mode, wins, losses, disabled | non |

---

## Signal Engine - 4 Couches

### Layer A: Tradeability (30%)
- 6 checks : volatilite, volume, spread, depth, funding, OI
- Kill switches configurables par version (V1 strict / V2 assoupli)

### Layer B: Direction (25%)
- EMA cross (20/50), market structure (HH/HL/LH/LL), RSI (14)
- Analyse sur timeframe superieur

### Layer C: Entry Triggers (25%)
- 4 setups : breakout, retest, divergence RSI, EMA bounce

### Layer D: Sentiment (20%)
- Analyse sentiment marche global

### Score Final
`score = tradeability(30%) + direction(25%) + setup(25%) + sentiment(20%)`

---

## Dashboard

### Onglets
1. **Bot V1** : Positions live V1 + signaux V1
2. **Bot V2** : Positions live V2 + signaux V2
3. **FT Freqtrade** : Positions ouvertes + historique FT
4. **Charts** : Candlestick temps reel + FVG + volume
5. **Journal** : Trades des 3 bots avec badges V1/V2/FT
6. **VS Comparaison** : 3 courbes P&L superposees (V1 bleu, V2 violet, FT orange) + stats
7. **Paires** : Paires surveillees

### Sidebar
3 sections : Bot V1 (bleu), Bot V2 (violet), Freqtrade (orange)
Chacune avec : status, balance, P&L, Win Rate, Gagnes, Perdus, Signaux, Trades

### Badges
- V1 = bleu `#58a6ff`
- V2 = violet `#bc8cff`
- FT = orange `#d29922`

---

## API Endpoints

### Signaux & Trades
- `GET /api/signals?limit=50&bot_version=V1` — Signaux
- `GET /api/trades?limit=50&bot_version=V1` — Journal trades
- `GET /api/stats?bot_version=V1` — Statistiques (win rate, PnL)
- `GET /api/pnl-history?bot_version=V1&days=30` — P&L cumule pour chart comparaison
- `GET /api/positions/live?bot_version=V1` — Positions avec prix temps reel

### Portfolio
- `GET /api/paper/portfolio?bot_version=V1` — Portfolio papier
- `POST /api/paper/reset?bot_version=V1` — Reset un bot (ou tous si pas de param)

### Marche
- `GET /api/ohlcv/{symbol}?timeframe=5m&limit=200` — Bougies OHLCV
- `GET /api/tickers` — Prix live toutes paires
- `GET /api/market/{symbol}` — Ticker + orderbook + funding
- `GET /api/pairs` — Paires actives
- `GET /api/balance` — Balance USDT (exchange prive)

### Freqtrade Proxy
- `GET /api/freqtrade/openTrades` — Positions FT ouvertes
- `GET /api/freqtrade/trades?limit=50` — Trades FT fermes
- `GET /api/freqtrade/stats` — Stats FT (balance, PnL, win rate)

### Autres
- `GET /api/status` — Etat des 2 scanners V1+V2
- `GET /api/debug/{symbol}?mode=scalping&bot_version=V2` — Debug analyse
- `POST /api/execute/{signal_id}` — Executer signal manuellement
- `GET /api/learning` — Stats apprentissage
- `GET /api/sentiment` — Sentiment marche
- `POST /api/config/reload` — Recharger configs
- `WS /ws/kline/{symbol}/{timeframe}` — WebSocket bougies temps reel

---

## Auth
- Page login : `/login` avec mot de passe simple
- Cookie session signe (itsdangerous, 30 jours)
- Middleware sur toutes les routes sauf `/login`, `/api/login`, `/health`, `/telegram/webhook`

---

## Freqtrade (bot externe)
- **URL** : https://freqtrade.swipego.app
- **API** : admin / Laurytal2
- **Coolify UUID** : `u0scsw0o08gwsoco0w0swk8k`
- **Config** : Binance Futures dry-run, 100$ USDT, 6 paires, 5m timeframe
- **Strategie** : CombinedStrategy (EMA9/21 + RSI + BB + ADX)

---

## Paires Tradees
XRP, DOGE, PEPE, RUNE, SOL, KAITO, BTC, VIRTUAL, TRUMP
(toutes en /USDT:USDT sur MEXC Futures)

---

## Points Critiques
- `order_executor.py` : `position_monitor` passe en parametre (pas d'import singleton)
- Cache version CSS/JS : incrementer a chaque modif frontend (actuellement **v=66**)
- MEXC API : exchange public (sans cle) pour data, cle API uniquement pour balance
- WebSocket : relay serveur obligatoire (navigateur bloque connexion directe MEXC)
- MEXC WS kline fields : `o/h/l/c` = prix, `q`/`a` = volume (PAS `v`)
- FQDN Coolify en `http://` (pas https) car Nginx gere SSL
- Windows : `git add -A` echoue (fichier `nul`), utiliser add explicite
- Git tags : `v1` (strict original), `v2`, `v2-loosened` (assoupli actuel)

---

## Erreurs Connues

| Probleme | Solution |
|----------|----------|
| Import singleton supprime -> crash deploy | Grep tous les imports avant de supprimer |
| ccxt version introuvable | Utiliser `ccxt==4.5.38` |
| MEXC API key IP restriction | Separer public / prive |
| Service Worker cache obsolete | Desactive, headers no-cache, cache-busting ?v=N |
| WebSocket navigateur bloque | Relay via serveur FastAPI |
| Chart zoom reset au refresh | `fitContent()` uniquement au 1er chargement |

---

## Paths Locaux (Windows)
- **Projet** : `C:\Users\asche\Downloads\claude\Cypto`
- **Python** : `C:\Users\asche\AppData\Local\Programs\Python\Python312\python.exe`
- **GitHub CLI** : `C:\Program Files\GitHub CLI\gh.exe`
