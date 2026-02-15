"""
Telegram Bot : envoi des signaux avec boutons EXECUTE/SKIP + webhook.
"""
import logging
import httpx
from app.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


async def send_message(text: str, parse_mode: str = "HTML", reply_markup: dict = None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram non configure")
        return None

    try:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": parse_mode,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{BASE_URL}/sendMessage",
                json=payload,
                timeout=10,
            )
            if resp.status_code != 200:
                logger.error(f"Telegram erreur: {resp.text}")
                return None
            data = resp.json()
            return data.get("result", {}).get("message_id")
    except Exception as e:
        logger.error(f"Telegram send erreur: {e}")
        return None


async def send_signal(signal: dict):
    """Envoie le signal compact avec boutons montant direct (1 clic = execute)."""
    direction = signal["direction"].upper()
    emoji = "\U0001f7e2" if direction == "LONG" else "\U0001f534"
    mode_label = "SCALP" if signal["mode"] == "scalping" else "SWING"

    entry = signal["entry_price"]
    dec = _get_decimals(entry)
    lev = signal.get("leverage", 10)
    signal_id = signal.get("id", 0)

    text = (
        f"{emoji} <b>{direction} {signal['symbol']}</b> [{mode_label}] "
        f"Score {signal['score']}\n"
        f"\u25b6 <code>{entry:.{dec}f}</code> | "
        f"SL <code>{signal['stop_loss']:.{dec}f}</code> | "
        f"TP <code>{signal['tp1']:.{dec}f}</code> / "
        f"<code>{signal['tp2']:.{dec}f}</code> / "
        f"<code>{signal['tp3']:.{dec}f}</code>\n"
        f"\U0001f527 {lev}x | {signal.get('setup_type', '')} "
        f"| R:R 1:{signal.get('rr_ratio', 0)}"
    )

    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "5$", "callback_data": f"go_5_{signal_id}"},
                {"text": "10$", "callback_data": f"go_10_{signal_id}"},
                {"text": "25$", "callback_data": f"go_25_{signal_id}"},
                {"text": "50$", "callback_data": f"go_50_{signal_id}"},
                {"text": "...$", "callback_data": f"cust_{signal_id}"},
            ],
            [
                {"text": "\U0001f4cb LIMIT", "callback_data": f"lmt_{signal_id}"},
                {"text": "\u274c", "callback_data": f"skip_{signal_id}"},
            ],
        ]
    }

    msg_id = await send_message(text, reply_markup=reply_markup)
    return msg_id


async def answer_callback_query(callback_id: str, text: str = ""):
    """Repond a un callback de bouton inline (toast discret, pas de popup)."""
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{BASE_URL}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text, "show_alert": False},
                timeout=10,
            )
    except Exception as e:
        logger.error(f"Erreur answer callback: {e}")


async def edit_message(chat_id: int, message_id: int, new_text: str):
    """Edite un message envoye (retire les boutons, met a jour le texte)."""
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{BASE_URL}/editMessageText",
                json={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": new_text,
                    "parse_mode": "HTML",
                },
                timeout=10,
            )
    except Exception as e:
        logger.error(f"Erreur edit message: {e}")


async def edit_message_reply_markup(chat_id: int, message_id: int, reply_markup: dict = None):
    """Retire ou modifie les boutons d'un message."""
    try:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        else:
            payload["reply_markup"] = {"inline_keyboard": []}

        async with httpx.AsyncClient() as client:
            await client.post(
                f"{BASE_URL}/editMessageReplyMarkup",
                json=payload,
                timeout=10,
            )
    except Exception as e:
        logger.error(f"Erreur edit reply markup: {e}")


async def send_execution_result(signal: dict, result: dict):
    """Envoie le resultat compact de l'execution."""
    if result["success"]:
        entry = result['actual_entry_price']
        dec = _get_decimals(float(entry)) if entry else 4
        is_limit = result.get("order_type") == "limit"
        sl_ok = "\u2705" if result.get('sl_order_id') else "\u274c"
        tp_count = sum(1 for t in result.get('tp_order_ids', []) if t)
        icon = "\U0001f4cb" if is_limit else "\U0001f680"
        label = "LIMIT" if is_limit else "MARKET"

        text = (
            f"{icon} <b>{label} {signal['direction'].upper()} {signal['symbol']}</b>\n"
            f"Entree <code>{entry:.{dec}f}</code> | {result['margin_required']}$ marge | "
            f"{result['position_size_usd']}$ pos\n"
            f"SL {sl_ok} | TP {tp_count}/3 \u2705 | "
            f"Balance: {result['balance']:.2f}$"
        )
    else:
        text = (
            f"\u274c <b>REFUSE</b> {signal['symbol']} {signal['direction'].upper()}\n"
            f"{result.get('error', 'Erreur')}"
        )

    await send_message(text)


async def send_no_trade_summary(results: list[dict]):
    if not results:
        return

    lines = []
    for r in results:
        score_pct = int(r.get("tradeability_score", 0) * 100)
        lines.append(f"  \u274c {r['symbol']} - {r.get('reason', 'N/A')} (score: {score_pct}%)")

    text = f"""\u26d4 <b>NON-TRADABLE</b> - {len(results)} paires filtrees

{chr(10).join(lines)}

\u23f3 Prochaine verification dans 30s"""

    await send_message(text)


async def send_startup_message():
    pairs = "N/A"
    try:
        from app.config import get_enabled_pairs
        pairs = ", ".join(get_enabled_pairs())
    except Exception:
        pass

    text = f"""\U0001f680 <b>Crypto Signals Bot demarre !</b>

\U0001f4e1 Mode : Paper Trading + Execution manuelle
\U0001f4ca Paires : {pairs}
\u23f0 Scan toutes les 30s
\U0001f50d Modes : Scalping + Swing
\U0001f3af Boutons EXECUTE sur chaque signal

\u2705 Systeme operationnel"""

    await send_message(text)


async def send_trade_update(symbol: str, update_type: str, details: str):
    emoji_map = {
        "tp1_hit": "\U0001f3af",
        "tp2_hit": "\U0001f3af\U0001f3af",
        "tp3_hit": "\U0001f389",
        "sl_hit": "\U0001f6d1",
        "break_even": "\U0001f6e1\ufe0f",
    }
    emoji = emoji_map.get(update_type, "\U0001f4e2")
    text = f"{emoji} <b>{symbol}</b> - {details}"
    await send_message(text)


async def register_webhook(webhook_url: str):
    """Enregistre le webhook Telegram."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{BASE_URL}/setWebhook",
                json={"url": webhook_url, "allowed_updates": ["callback_query", "message"]},
                timeout=10,
            )
            data = resp.json()
            if data.get("ok"):
                logger.info(f"Telegram webhook enregistre: {webhook_url}")
            else:
                logger.error(f"Telegram webhook erreur: {data}")
    except Exception as e:
        logger.error(f"Erreur register webhook: {e}")


async def delete_webhook():
    """Supprime le webhook Telegram."""
    try:
        async with httpx.AsyncClient() as client:
            await client.post(f"{BASE_URL}/deleteWebhook", timeout=10)
    except Exception:
        pass


def _get_decimals(price: float) -> int:
    if price >= 100:
        return 2
    elif price >= 1:
        return 4
    elif price >= 0.01:
        return 6
    else:
        return 8
