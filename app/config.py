import os
import yaml
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "settings.yaml"
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)


def load_settings(path=None) -> dict:
    p = path or CONFIG_PATH
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# Charger les 3 configs V1, V2 et V3
SETTINGS_V1 = load_settings(BASE_DIR / "config" / "settings_V1.yaml")
SETTINGS_V2 = load_settings(BASE_DIR / "config" / "settings_V2.yaml")
SETTINGS_V3 = load_settings(BASE_DIR / "config" / "settings_V3.yaml")
SETTINGS = SETTINGS_V2  # Default retro-compat

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


def get_enabled_pairs(settings=None) -> list[str]:
    s = settings or SETTINGS
    return [p["symbol"] for p in s["pairs"] if p.get("enabled", True)]


def get_mode_config(mode: str, settings=None) -> dict:
    s = settings or SETTINGS
    return s.get(mode, {})


def reload_settings():
    global SETTINGS, SETTINGS_V1, SETTINGS_V2, SETTINGS_V3
    SETTINGS_V1 = load_settings(BASE_DIR / "config" / "settings_V1.yaml")
    SETTINGS_V2 = load_settings(BASE_DIR / "config" / "settings_V2.yaml")
    SETTINGS_V3 = load_settings(BASE_DIR / "config" / "settings_V3.yaml")
    SETTINGS = SETTINGS_V2
