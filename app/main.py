"""
Point d'entree principal : FastAPI + Scanner + Dashboard.
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

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
