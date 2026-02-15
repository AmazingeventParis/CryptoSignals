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
    """Envoie le signal avec boutons EXECUTE / SKIP."""
    direction = signal["direction"].upper()
    emoji = "\U0001f7e2" if direction == "LONG" else "\U0001f534"
    mode_label = "SCALPING" if signal["mode"] == "scalping" else "SWING"

    score = signal["score"]
    if score >= 80:
        score_badge = "\U0001f525"
    elif score >= 65:
        score_badge = "\u26a1"
    else:
        score_badge = "\u2139\ufe0f"

    entry = signal["entry_price"]
    decimals = _get_decimals(entry)

    reasons_text = "\n".join(f"  \u2022 {r}" for r in signal.get("reasons", []))
    lev = signal.get("leverage", 10)
    signal_id = signal.get("id", 0)

    text = f"""{'━' * 25}
{emoji} <b>{direction}  {signal['symbol']}</b>  [{mode_label}]
{'━' * 25}
{score_badge} <b>Score : {score}/100</b>
\U0001f4ca Setup : {signal.get('setup_type', 'N/A')}

\u25b6 Entree  : <code>{entry:.{decimals}f}</code>
\U0001f6d1 Stop   : <code>{signal['stop_loss']:.{decimals}f}</code> ({signal.get('risk_pct', 0):.2f}%)
\u2705 TP1    : <code>{signal['tp1']:.{decimals}f}</code> (fermer {signal.get('tp1_close_pct', 40)}%)
\u2705 TP2    : <code>{signal['tp2']:.{decimals}f}</code> (fermer {signal.get('tp2_close_pct', 30)}%)
\u2705 TP3    : <code>{signal['tp3']:.{decimals}f}</code> (fermer {signal.get('tp3_close_pct', 30)}%)

\U0001f4d0 R:R    : 1:{signal.get('rr_ratio', 0)}
\U0001f4b0 Risque : 1% du capital
\U0001f527 Levier : {lev}x isole

\U0001f4cb <b>Raisons :</b>
{reasons_text}

\u23f1 Break-even au TP1
\u26a0\ufe0f <i>Clique EXECUTE pour placer l'ordre sur MEXC.</i>
{'━' * 25}"""

    # Boutons inline
    reply_markup = {
        "inline_keyboard": [[
            {"text": "\u2705 EXECUTE", "callback_data": f"exec_{signal_id}"},
            {"text": "\u274c SKIP", "callback_data": f"skip_{signal_id}"},
        ]]
    }

    msg_id = await send_message(text, reply_markup=reply_markup)
    return msg_id


async def answer_callback_query(callback_id: str, text: str):
    """Repond a un callback de bouton inline."""
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{BASE_URL}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text, "show_alert": True},
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
    """Envoie le resultat de l'execution."""
    if result["success"]:
        entry = result['actual_entry_price']
        decimals = _get_decimals(float(entry)) if entry else 4

        text = f"""\U0001f680 <b>ORDRE EXECUTE !</b>

\U0001f4b9 {signal['symbol']} {signal['direction'].upper()}
\U0001f4b0 Entree reelle : <code>{entry}</code>
\U0001f4b0 Signal etait  : <code>{signal.get('entry_price', 'N/A')}</code>
\U0001f4e6 Quantite : {result['quantity']}
\U0001f4b5 Position : {result['position_size_usd']}$
\U0001f512 Marge : {result['margin_required']}$

\U0001f6d1 SL place : {'Oui' if result.get('sl_order_id') else 'Non'}
\u2705 TPs places : {sum(1 for t in result.get('tp_order_ids', []) if t)}/3

\U0001f3e6 Balance restante : {result['balance']:.2f} USDT

\u26a0\ufe0f <i>SL et TPs sont actifs. Va sur MEXC pour verifier.</i>"""
    else:
        text = f"""\u274c <b>EXECUTION REFUSEE</b>

{signal['symbol']} {signal['direction'].upper()}
\u26a0\ufe0f {result.get('error', 'Erreur inconnue')}

<i>Le prix a peut-etre trop bouge depuis le signal.
Attends le prochain signal.</i>"""

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
