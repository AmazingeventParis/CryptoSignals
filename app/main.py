"""
Point d'entree principal : FastAPI + Scanner + Dashboard.
"""
import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from starlette.middleware.base import BaseHTTPMiddleware
import websockets

from app.config import SETTINGS, LOG_LEVEL, BASE_DIR, TELEGRAM_CHAT_ID
from app.database import init_db, get_signal_by_id, update_signal_status, get_latest_active_signal
from app.core.market_data import market_data
from app.core.scanner import scanner
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Demarrage Crypto Signals Bot...")
    await init_db()

    # Connexion MEXC non bloquante
    await market_data.connect()
    if market_data.is_connected():
        logger.info("MEXC connecte - scanner demarre")
    else:
        logger.warning("MEXC non connecte - dashboard seul, le scanner retentera la connexion")

    try:
        await send_startup_message()
    except Exception as e:
        logger.warning(f"Telegram startup message echoue: {e}")

    # Enregistrer le webhook Telegram pour les boutons
    try:
        await register_webhook("https://crypto.swipego.app/telegram/webhook")
    except Exception as e:
        logger.warning(f"Telegram webhook registration echoue: {e}")

    # Lancer le scanner en background
    scanner_task = asyncio.create_task(scanner.start())
    logger.info("Scanner lance en arriere-plan")

    yield

    # Shutdown
    logger.info("Arret du bot...")
    await scanner.stop()
    scanner_task.cancel()
    await market_data.close()


app = FastAPI(
    title="Crypto Signals MEXC",
    description="Bot de signaux trading crypto pour MEXC Futures",
    version="1.0.0",
    lifespan=lifespan,
)

# No-cache pour fichiers statiques
class NoCacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if '/static/' in str(request.url):
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        return response

app.add_middleware(NoCacheMiddleware)

# API routes
app.include_router(router)

# Dashboard static files
static_dir = BASE_DIR / "app" / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/")
async def dashboard():
    return FileResponse(str(static_dir / "index.html"))


@app.get("/health")
async def health():
    return {"status": "ok", "scanner_running": scanner.running}


# --- Telegram Webhook ---
# pending : pour LIMIT (attend montant libre)
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

    # --- Callback de bouton ---
    callback = update.get("callback_query")
    if callback:
        callback_id = callback.get("id")
        data = callback.get("data", "")
        chat_id = callback.get("message", {}).get("chat", {}).get("id")
        message_id = callback.get("message", {}).get("message_id")

        if str(chat_id) != str(TELEGRAM_CHAT_ID):
            return {"ok": True}

        # === GO : 1 clic = montant + MARKET direct ===
        if data.startswith("go_"):
            parts = data.split("_")
            margin_usdt = float(parts[1])
            signal_id = int(parts[2])
            await answer_callback_query(callback_id)
            await edit_message_reply_markup(chat_id, message_id)
            await _do_execute(str(chat_id), signal_id, "market", margin_usdt=margin_usdt)

        # === LIMIT : demande montant puis place ordre limit ===
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

        # === LIMIT GO : montant choisi -> place ordre limit ===
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

    # --- Message texte : montant libre ---
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
                    # En attente LIMIT
                    signal_id = pending["signal_id"]
                    await _do_execute(chat_id, signal_id, "limit", margin_usdt=margin_usdt)
                else:
                    # Pas de pending -> MARKET sur le dernier signal actif
                    latest = await get_latest_active_signal()
                    if latest:
                        await _do_execute(chat_id, latest["id"], "market", margin_usdt=margin_usdt)

    return {"ok": True}


async def _do_execute(
    chat_id: str, signal_id: int, order_type: str, margin_usdt: float = 10,
):
    """Execute l'ordre (market ou limit) avec le montant choisi."""
    from app.services.telegram_bot import send_message, send_execution_result

    # Recuperer le signal
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
        # SIMULATION
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

    # EXECUTION REELLE
    result = await execute_signal(signal_data, margin_usdt=margin_usdt, order_type=order_type)

    new_status = "executed" if result["success"] else "error"
    await update_signal_status(signal_id, new_status)

    await send_execution_result(signal_data, result)
    logger.info(f"Signal {signal_id} {order_type} {margin_usdt}$ : {result.get('success')}")


# --- WebSocket relay MEXC temps reel ---
TF_MAP = {'1m': 'Min1', '3m': 'Min3', '5m': 'Min5', '15m': 'Min15', '1h': 'Min60', '4h': 'Hour4'}


@app.websocket("/ws/kline/{symbol}/{timeframe}")
async def kline_ws(websocket: WebSocket, symbol: str, timeframe: str):
    await websocket.accept()
    mexc_tf = TF_MAP.get(timeframe, 'Min5')
    # "XRP-USDT:USDT" -> "XRP_USDT"
    mexc_symbol = symbol.split(':')[0].replace('-', '_').replace('/', '_')

    logger.info(f"WS client connecte: {mexc_symbol} {mexc_tf}")
    try:
        async with websockets.connect('wss://contract.mexc.com/edge') as mexc_ws:
            # Subscribe kline
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
                # Detecter deconnexion client
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
