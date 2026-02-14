"""
Point d'entree principal : FastAPI + Scanner + Dashboard.
"""
import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import websockets

from app.config import SETTINGS, LOG_LEVEL, BASE_DIR
from app.database import init_db
from app.core.market_data import market_data
from app.core.scanner import scanner
from app.api.routes import router
from app.services.telegram_bot import send_startup_message

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
