"""
Point d'entree principal : FastAPI + Scanner V1/V2 + Dashboard.
"""
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
import websockets

from app.config import SETTINGS, SETTINGS_V1, SETTINGS_V2, SETTINGS_V3, SETTINGS_V4, LOG_LEVEL, BASE_DIR, TELEGRAM_CHAT_ID
from app.core.adaptive_learner import AdaptiveLearner
from app.core.signal_engine import register_adaptive_learner

# Auth config
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "default-secret-change-me")
SESSION_MAX_AGE = 30 * 24 * 3600  # 30 jours
_serializer = URLSafeTimedSerializer(SESSION_SECRET)
from app.database import init_db, get_signal_by_id, update_signal_status
from app.core.market_data import market_data
from app.core.scanner import Scanner
from app.core.position_monitor import PositionMonitor
from app.core.paper_trader import PaperTrader
from app.core.order_executor import execute_signal
from app.api.routes import router
from app.services.telegram_bot import (
    send_startup_message, register_webhook, answer_callback_query,
    edit_message_reply_markup, send_execution_result,
)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# --- Instanciation des 4 bots ---
scanner_v1 = Scanner("V1", SETTINGS_V1)
scanner_v2 = Scanner("V2", SETTINGS_V2)
scanner_v3 = Scanner("V3", SETTINGS_V3)
scanner_v4 = Scanner("V4", SETTINGS_V4)
paper_trader_v1 = PaperTrader("V1", SETTINGS_V1)
paper_trader_v2 = PaperTrader("V2", SETTINGS_V2)
paper_trader_v3 = PaperTrader("V3", SETTINGS_V3)
paper_trader_v4 = PaperTrader("V4", SETTINGS_V4)
position_monitor_v1 = PositionMonitor("V1", SETTINGS_V1)
position_monitor_v2 = PositionMonitor("V2", SETTINGS_V2)
position_monitor_v3 = PositionMonitor("V3", SETTINGS_V3)
position_monitor_v4 = PositionMonitor("V4", SETTINGS_V4)

# --- V4 only: AdaptiveLearner ---
adaptive_learner_v4 = AdaptiveLearner("V4")
register_adaptive_learner("V4", adaptive_learner_v4)

# Connecter les composants entre eux
paper_trader_v1.set_position_monitor(position_monitor_v1)
paper_trader_v2.set_position_monitor(position_monitor_v2)
paper_trader_v3.set_position_monitor(position_monitor_v3)
paper_trader_v4.set_position_monitor(position_monitor_v4)
scanner_v1.set_paper_trader(paper_trader_v1)
scanner_v2.set_paper_trader(paper_trader_v2)
scanner_v3.set_paper_trader(paper_trader_v3)
scanner_v4.set_paper_trader(paper_trader_v4)
scanner_v1.set_position_monitor(position_monitor_v1)
scanner_v2.set_position_monitor(position_monitor_v2)
scanner_v3.set_position_monitor(position_monitor_v3)
scanner_v4.set_position_monitor(position_monitor_v4)
# V4 only: wire adaptive learner
position_monitor_v4.set_adaptive_learner(adaptive_learner_v4)

# Export pour routes.py
bot_instances = {
    "V1": {"scanner": scanner_v1, "paper_trader": paper_trader_v1, "position_monitor": position_monitor_v1},
    "V2": {"scanner": scanner_v2, "paper_trader": paper_trader_v2, "position_monitor": position_monitor_v2},
    "V3": {"scanner": scanner_v3, "paper_trader": paper_trader_v3, "position_monitor": position_monitor_v3},
    "V4": {"scanner": scanner_v4, "paper_trader": paper_trader_v4, "position_monitor": position_monitor_v4, "adaptive_learner": adaptive_learner_v4},
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Demarrage Crypto Signals Bot (V1 + V2 + V3 + V4)...")
    await init_db()

    # Connexion MEXC (partagee)
    await market_data.connect()
    if market_data.is_connected():
        logger.info("MEXC connecte - scanners demarrent")
    else:
        logger.warning("MEXC non connecte - dashboard seul, le scanner retentera la connexion")

    # V4 only: Charger le cache adaptive learner
    await adaptive_learner_v4.refresh_cache()
    logger.info("Adaptive Learner V4 cache charge")

    # Demarrer les paper traders (portefeuilles fictifs)
    await paper_trader_v1.start()
    await paper_trader_v2.start()
    await paper_trader_v3.start()
    await paper_trader_v4.start()
    logger.info("Paper Traders V1+V2+V3+V4 demarres")

    # Lancer les scanners
    scanner_v1_task = asyncio.create_task(scanner_v1.start())
    scanner_v2_task = asyncio.create_task(scanner_v2.start())
    scanner_v3_task = asyncio.create_task(scanner_v3.start())
    scanner_v4_task = asyncio.create_task(scanner_v4.start())
    logger.info("Scanners V1+V2+V3+V4 lances en arriere-plan")

    # Lancer les position monitors
    monitor_v1_task = asyncio.create_task(position_monitor_v1.start())
    monitor_v2_task = asyncio.create_task(position_monitor_v2.start())
    monitor_v3_task = asyncio.create_task(position_monitor_v3.start())
    monitor_v4_task = asyncio.create_task(position_monitor_v4.start())
    logger.info("Position Monitors V1+V2+V3+V4 lances en arriere-plan")

    yield

    # Shutdown
    logger.info("Arret du bot...")
    await scanner_v1.stop()
    await scanner_v2.stop()
    await scanner_v3.stop()
    await scanner_v4.stop()
    scanner_v1_task.cancel()
    scanner_v2_task.cancel()
    scanner_v3_task.cancel()
    scanner_v4_task.cancel()
    await position_monitor_v1.stop()
    await position_monitor_v2.stop()
    await position_monitor_v3.stop()
    await position_monitor_v4.stop()
    monitor_v1_task.cancel()
    monitor_v2_task.cancel()
    monitor_v3_task.cancel()
    monitor_v4_task.cancel()
    await market_data.close()


app = FastAPI(
    title="Crypto Signals MEXC",
    description="Bot de signaux trading crypto pour MEXC Futures (V1 + V2 + V3 + V4)",
    version="4.0.0",
    lifespan=lifespan,
)

# --- Auth helpers ---
def _check_session(request: Request) -> bool:
    token = request.cookies.get("session")
    if not token:
        return False
    try:
        _serializer.loads(token, max_age=SESSION_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


_PUBLIC_PATHS = {"/login", "/api/login", "/health", "/telegram/webhook"}
_PUBLIC_PREFIXES = ("/static/login.html",)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path

        if path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES):
            return await call_next(request)

        if not DASHBOARD_PASSWORD:
            return await call_next(request)

        if _check_session(request):
            return await call_next(request)

        if path.startswith("/api/") or path.startswith("/ws/"):
            return JSONResponse({"error": "Non authentifie"}, status_code=401)

        return RedirectResponse("/login", status_code=302)


class NoCacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        path = str(request.url.path)
        if '/static/' in path or path == '/':
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        return response

app.add_middleware(NoCacheMiddleware)
app.add_middleware(AuthMiddleware)

app.include_router(router)

static_dir = BASE_DIR / "app" / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/login")
async def login_page():
    return FileResponse(str(static_dir / "login.html"))


@app.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    password = body.get("password", "")
    if password == DASHBOARD_PASSWORD:
        token = _serializer.dumps("authenticated")
        response = JSONResponse({"ok": True})
        response.set_cookie(
            key="session",
            value=token,
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
        )
        return response
    return JSONResponse({"error": "Mot de passe incorrect"}, status_code=401)


@app.post("/api/logout")
async def api_logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie("session")
    return response


@app.get("/")
async def dashboard():
    response = FileResponse(str(static_dir / "index.html"))
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "2026-02-27-v71",
        "scanner_v1_running": scanner_v1.running,
        "scanner_v2_running": scanner_v2.running,
        "scanner_v3_running": scanner_v3.running,
        "scanner_v4_running": scanner_v4.running,
    }


# --- Telegram Webhook ---
pending_executions: dict = {}


def _get_decimals(price: float) -> int:
    if price >= 100:
        return 2
    elif price >= 1:
        return 4
    elif price >= 0.01:
        return 6
    return 8


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    try:
        update = await request.json()
    except Exception:
        return {"ok": True}

    callback = update.get("callback_query")
    if callback:
        callback_id = callback.get("id")
        data = callback.get("data", "")
        chat_id = callback.get("message", {}).get("chat", {}).get("id")
        message_id = callback.get("message", {}).get("message_id")

        if str(chat_id) != str(TELEGRAM_CHAT_ID):
            return {"ok": True}

        if data.startswith("go_"):
            parts = data.split("_")
            margin_usdt = float(parts[1])
            signal_id = int(parts[2])
            await answer_callback_query(callback_id)
            await edit_message_reply_markup(chat_id, message_id)
            await _do_execute(str(chat_id), signal_id, "market", margin_usdt=margin_usdt)

        elif data.startswith("lmt_"):
            signal_id = int(data.split("_")[1])
            signal = await get_signal_by_id(signal_id)
            if not signal:
                await answer_callback_query(callback_id)
                return {"ok": True}

            await answer_callback_query(callback_id)
            await edit_message_reply_markup(chat_id, message_id)

            import json as _json
            signal_data = {
                **signal,
                "reasons": _json.loads(signal.get("reasons", "[]")) if isinstance(signal.get("reasons"), str) else signal.get("reasons", []),
            }
            pending_executions[str(chat_id)] = {
                "signal_id": signal_id,
                "signal": signal_data,
                "step": "limit_amount",
            }

            from app.services.telegram_bot import send_message
            dec = _get_decimals(signal["entry_price"])
            lev = signal.get("leverage", 10)
            await send_message(
                f"\U0001f4cb <b>LIMIT @ {signal['entry_price']:.{dec}f}</b> | Levier {lev}x\n"
                f"\u2b07\ufe0f Marge ? Bouton ou tape montant",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {"text": "5$", "callback_data": f"lgo_5_{signal_id}"},
                            {"text": "10$", "callback_data": f"lgo_10_{signal_id}"},
                            {"text": "25$", "callback_data": f"lgo_25_{signal_id}"},
                            {"text": "50$", "callback_data": f"lgo_50_{signal_id}"},
                        ],
                        [{"text": "\u274c Annuler", "callback_data": f"cancel_{signal_id}"}],
                    ]
                },
            )

        elif data.startswith("cust_"):
            signal_id = int(data.split("_")[1])
            signal = await get_signal_by_id(signal_id)
            if not signal:
                await answer_callback_query(callback_id)
                return {"ok": True}

            await answer_callback_query(callback_id)
            await edit_message_reply_markup(chat_id, message_id)

            pending_executions[str(chat_id)] = {
                "signal_id": signal_id,
                "step": "custom_market",
            }

            from app.services.telegram_bot import send_message
            await send_message(
                "\U0001f4b2 <b>Montant MARKET ?</b>\nTape le montant en $ (ex: 15)"
            )

        elif data.startswith("lgo_"):
            parts = data.split("_")
            margin_usdt = float(parts[1])
            signal_id = int(parts[2])
            await answer_callback_query(callback_id)
            await edit_message_reply_markup(chat_id, message_id)
            pending_executions.pop(str(chat_id), None)
            await _do_execute(str(chat_id), signal_id, "limit", margin_usdt=margin_usdt)

        elif data.startswith("skip_"):
            signal_id = int(data.split("_")[1])
            await update_signal_status(signal_id, "skipped")
            await edit_message_reply_markup(chat_id, message_id)
            await answer_callback_query(callback_id, "Signal ignore")

        elif data.startswith("cancel_"):
            signal_id = int(data.split("_")[1])
            pending_executions.pop(str(chat_id), None)
            await edit_message_reply_markup(chat_id, message_id)
            await answer_callback_query(callback_id, "Annule")

        return {"ok": True}

    message = update.get("message")
    if message:
        chat_id = str(message.get("chat", {}).get("id"))
        text = (message.get("text") or "").strip()

        if chat_id != str(TELEGRAM_CHAT_ID):
            return {"ok": True}

        if text:
            import re
            match = re.match(r"^(\d+\.?\d*)", text.replace(",", "."))
            if match:
                margin_usdt = float(match.group(1))
                pending = pending_executions.pop(chat_id, None)
                if pending:
                    signal_id = pending["signal_id"]
                    order_type = "limit" if pending.get("step") == "limit_amount" else "market"
                    await _do_execute(chat_id, signal_id, order_type, margin_usdt=margin_usdt)

    return {"ok": True}


async def _do_execute(
    chat_id: str, signal_id: int, order_type: str, margin_usdt: float = 10,
):
    from app.services.telegram_bot import send_message, send_execution_result

    signal_db = await get_signal_by_id(signal_id)
    if not signal_db:
        await send_message("\u274c Signal introuvable")
        return

    import json as _json
    signal_data = {
        **signal_db,
        "reasons": _json.loads(signal_db.get("reasons", "[]")) if isinstance(signal_db.get("reasons"), str) else signal_db.get("reasons", []),
    }

    if signal_db.get("status") == "executed":
        await send_message("\u274c Deja execute")
        return

    lev = signal_data.get("leverage", 10)
    is_test = signal_db.get("status") == "test"

    if is_test:
        position_usd = margin_usdt * lev
        fake_result = {
            "success": True,
            "order_type": order_type,
            "entry_order_id": "TEST",
            "actual_entry_price": signal_data["entry_price"],
            "sl_order_id": "TEST",
            "tp_order_ids": ["TEST", "TEST", "TEST"],
            "quantity": round(position_usd / signal_data["entry_price"], 6),
            "position_size_usd": position_usd,
            "margin_required": margin_usdt,
            "balance": 100.0,
        }
        await send_execution_result(signal_data, fake_result)
        await send_message("\U0001f9ea <b>SIMULATION</b> - Aucun ordre place")
        return

    bv = signal_data.get("bot_version", "V2")
    pm = bot_instances.get(bv, bot_instances["V2"])["position_monitor"]
    result = await execute_signal(signal_data, margin_usdt=margin_usdt, order_type=order_type, position_monitor=pm)

    new_status = "executed" if result["success"] else "error"
    await update_signal_status(signal_id, new_status)

    await send_execution_result(signal_data, result)
    logger.info(f"Signal {signal_id} {order_type} {margin_usdt}$ : {result.get('success')}")


# --- WebSocket relay MEXC temps reel ---
TF_MAP = {'1m': 'Min1', '3m': 'Min3', '5m': 'Min5', '15m': 'Min15', '1h': 'Min60', '4h': 'Hour4', '1d': 'Day1', '1w': 'Week1'}


@app.websocket("/ws/kline/{symbol}/{timeframe}")
async def kline_ws(websocket: WebSocket, symbol: str, timeframe: str):
    await websocket.accept()
    mexc_tf = TF_MAP.get(timeframe, 'Min5')
    mexc_symbol = symbol.split(':')[0].replace('-', '_').replace('/', '_')

    logger.info(f"WS client connecte: {mexc_symbol} {mexc_tf}")
    try:
        async with websockets.connect('wss://contract.mexc.com/edge') as mexc_ws:
            await mexc_ws.send(json.dumps({
                'method': 'sub.kline',
                'param': {'symbol': mexc_symbol, 'interval': mexc_tf}
            }))

            async def forward_mexc():
                async for raw in mexc_ws:
                    msg = json.loads(raw)
                    if msg.get('channel') == 'push.kline' and msg.get('data'):
                        await websocket.send_json(msg['data'])

            async def keep_alive():
                while True:
                    await asyncio.sleep(20)
                    await mexc_ws.send('{"method":"ping"}')

            async def listen_client():
                async for _ in websocket.iter_text():
                    pass

            done, pending = await asyncio.wait(
                [asyncio.create_task(forward_mexc()),
                 asyncio.create_task(keep_alive()),
                 asyncio.create_task(listen_client())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
    except (WebSocketDisconnect, Exception) as e:
        logger.info(f"WS client deconnecte: {mexc_symbol} - {e}")


@app.websocket("/ws/positions")
async def positions_ws(websocket: WebSocket):
    """WebSocket temps reel : stream les prix et P&L des positions actives (V1+V2+V3+V4)."""
    await websocket.accept()
    logger.info("WS positions client connecte")

    try:
        while True:
            # Combiner positions V1, V2, V3 et V4
            positions_v1 = list(position_monitor_v1._positions.values())
            positions_v2 = list(position_monitor_v2._positions.values())
            positions_v3 = list(position_monitor_v3._positions.values())
            positions_v4 = list(position_monitor_v4._positions.values())
            all_positions = positions_v1 + positions_v2 + positions_v3 + positions_v4
            active = [p for p in all_positions if p.get("state") != "closed"]

            if not active:
                await websocket.send_json({"positions": []})
                await asyncio.sleep(2)
                continue

            symbols = list(set(p["symbol"] for p in active))
            mexc_symbols = {}
            for s in symbols:
                mexc_symbols[s] = s.split(":")[0].replace("-", "_").replace("/", "_")

            async with websockets.connect("wss://contract.mexc.com/edge") as mexc_ws:
                for s, ms in mexc_symbols.items():
                    await mexc_ws.send(json.dumps({
                        "method": "sub.deal",
                        "param": {"symbol": ms}
                    }))

                prices: dict[str, float] = {}
                ping_task = asyncio.create_task(_ws_ping(mexc_ws))
                listen_task = asyncio.create_task(_ws_listen_client(websocket))

                try:
                    async for raw in mexc_ws:
                        if listen_task.done():
                            break

                        msg = json.loads(raw)
                        if msg.get("channel") == "push.deal" and msg.get("data"):
                            deals = msg["data"]
                            last_deal = deals[-1] if isinstance(deals, list) and deals else deals if isinstance(deals, dict) else {}
                            price = float(last_deal.get("p", 0))
                            sym_key = msg.get("symbol", "")
                            if price > 0:
                                for s, ms in mexc_symbols.items():
                                    if ms == sym_key:
                                        prices[s] = price
                                        break

                                result = []
                                for p in active:
                                    cur = prices.get(p["symbol"], 0)
                                    if cur == 0:
                                        continue
                                    pnl_data = _calc_live_pnl(p, cur)
                                    result.append(pnl_data)

                                if result:
                                    await websocket.send_json({"positions": result})

                        # Re-checker les positions actives
                        new_v1 = list(position_monitor_v1._positions.values())
                        new_v2 = list(position_monitor_v2._positions.values())
                        new_v3 = list(position_monitor_v3._positions.values())
                        new_v4 = list(position_monitor_v4._positions.values())
                        new_active = [p for p in new_v1 + new_v2 + new_v3 + new_v4 if p.get("state") != "closed"]
                        new_symbols = set(p["symbol"] for p in new_active)
                        if new_symbols != set(symbols):
                            break

                finally:
                    ping_task.cancel()
                    listen_task.cancel()

    except (WebSocketDisconnect, Exception) as e:
        logger.info(f"WS positions deconnecte: {e}")


async def _ws_ping(ws):
    while True:
        await asyncio.sleep(20)
        try:
            await ws.send('{"method":"ping"}')
        except Exception:
            break


async def _ws_listen_client(websocket: WebSocket):
    try:
        async for _ in websocket.iter_text():
            pass
    except Exception:
        pass


def _calc_live_pnl(pos: dict, current_price: float) -> dict:
    entry = pos["entry_price"]
    direction = pos["direction"]
    original_qty = pos["original_quantity"]
    remaining_qty = pos["remaining_quantity"]

    realized = 0.0
    if pos.get("tp1_hit"):
        tp1_qty = original_qty * (pos.get("tp1_close_pct", 40) / 100)
        diff = (pos["tp1"] - entry) if direction == "long" else (entry - pos["tp1"])
        realized += diff * tp1_qty
    if pos.get("tp2_hit"):
        tp2_qty = original_qty * (pos.get("tp2_close_pct", 30) / 100)
        diff = (pos["tp2"] - entry) if direction == "long" else (entry - pos["tp2"])
        realized += diff * tp2_qty

    diff = (current_price - entry) if direction == "long" else (entry - current_price)
    unrealized = diff * remaining_qty
    total = realized + unrealized
    margin = pos.get("margin_required", 1) or 1
    pnl_pct = (total / margin) * 100

    sl = pos["stop_loss"]
    tp3 = pos["tp3"]
    if direction == "long":
        progress = max(0, min(100, ((current_price - sl) / (tp3 - sl)) * 100)) if tp3 != sl else 50
    else:
        progress = max(0, min(100, ((sl - current_price) / (sl - tp3)) * 100)) if tp3 != sl else 50

    return {
        "id": pos["id"],
        "symbol": pos["symbol"],
        "direction": direction,
        "entry_price": entry,
        "current_price": round(current_price, 8),
        "stop_loss": pos["stop_loss"],
        "tp1": pos["tp1"],
        "tp2": pos["tp2"],
        "tp3": pos["tp3"],
        "tp1_hit": pos.get("tp1_hit", 0),
        "tp2_hit": pos.get("tp2_hit", 0),
        "tp3_hit": pos.get("tp3_hit", 0),
        "state": pos.get("state", "active"),
        "margin_required": pos.get("margin_required", 0),
        "total_pnl": round(total, 4),
        "pnl_pct": round(pnl_pct, 2),
        "progress": round(progress, 1),
        "bot_version": pos.get("bot_version", "V2"),
    }
