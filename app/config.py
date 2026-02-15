import os
import yaml
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "settings.yaml"
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)


def load_settings() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


SETTINGS = load_settings()

# Env vars
MEXC_API_KEY = os.getenv("MEXC_API_KEY", "")
MEXC_SECRET_KEY = os.getenv("MEXC_SECRET_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
APP_MODE = os.getenv("APP_MODE", "paper")  # paper | live
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# API keys sentiment (optionnels)
CRYPTOPANIC_TOKEN = os.getenv("CRYPTOPANIC_TOKEN", "")
FINNHUB_TOKEN = os.getenv("FINNHUB_TOKEN", "")

DB_PATH = DATA_DIR / "signals.db"


def get_enabled_pairs() -> list[str]:
    return [p["symbol"] for p in SETTINGS["pairs"] if p.get("enabled", True)]


def get_mode_config(mode: str) -> dict:
    return SETTINGS.get(mode, {})


def reload_settings():
    global SETTINGS
    SETTINGS = load_settings()
