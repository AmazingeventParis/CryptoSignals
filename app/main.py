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
from app.database import init_db, get_signal_by_id, update_signal_status
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


# --- Telegram Webhook : boutons EXECUTE/SKIP ---
@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    try:
        update = await request.json()
    except Exception:
        return {"ok": True}

    callback = update.get("callback_query")
    if not callback:
        return {"ok": True}

    callback_id = callback.get("id")
    data = callback.get("data", "")
    chat_id = callback.get("message", {}).get("chat", {}).get("id")
    message_id = callback.get("message", {}).get("message_id")

    # Verifier que c'est bien notre chat
    if str(chat_id) != str(TELEGRAM_CHAT_ID):
        return {"ok": True}

    if data.startswith("exec_"):
        signal_id = int(data.split("_")[1])
        signal = await get_signal_by_id(signal_id)

        if not signal:
            await answer_callback_query(callback_id, "Signal introuvable")
            return {"ok": True}

        if signal["status"] == "executed":
            await answer_callback_query(callback_id, "Deja execute !")
            return {"ok": True}

        # Retirer les boutons immediatement
        await edit_message_reply_markup(chat_id, message_id)
        await answer_callback_query(callback_id, "Execution en cours...")

        # Executer l'ordre
        import json as _json
        signal_data = {
            **signal,
            "reasons": _json.loads(signal.get("reasons", "[]")) if isinstance(signal.get("reasons"), str) else signal.get("reasons", []),
        }
        result = await execute_signal(signal_data)

        # Mettre a jour le statut
        new_status = "executed" if result["success"] else "error"
        await update_signal_status(signal_id, new_status)

        # Envoyer le resultat
        await send_execution_result(signal_data, result)
        logger.info(f"Signal {signal_id} execute: {result.get('success')}")

    elif data.startswith("skip_"):
        signal_id = int(data.split("_")[1])
        await update_signal_status(signal_id, "skipped")
        await edit_message_reply_markup(chat_id, message_id)
        await answer_callback_query(callback_id, "Signal ignore")

    return {"ok": True}


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
