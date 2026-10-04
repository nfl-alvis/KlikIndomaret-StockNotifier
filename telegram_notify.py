"""Pengirim notifikasi Telegram Bot API."""

from __future__ import annotations

import html
import logging

import requests

log = logging.getLogger("telegram")

TELEGRAM_API = "https://api.telegram.org"


def escape_html(text: str) -> str:
    return html.escape(text, quote=False)


def send_message(bot_token: str, chat_id: str, text: str, parse_mode: str = "HTML") -> bool:
    """Kirim pesan. Return True bila sukses."""
    url = f"{TELEGRAM_API}/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(url, json=payload, timeout=20)
    except requests.RequestException as e:
        log.error("Gagal mengirim ke Telegram: %s", e)
        return False
    if resp.status_code == 429:
        retry = resp.json().get("parameters", {}).get("retry_after", 3)
        log.warning("Telegram rate limit, coba lagi dalam %s detik", retry)
        return False
    if resp.status_code != 200:
        log.error("Telegram HTTP %s: %s", resp.status_code, resp.text[:200])
        return False
    ok = resp.json().get("ok") is True
    if not ok:
        log.error("Telegram menolak pesan: %s", resp.text[:200])
    return ok


def test(bot_token: str, chat_id: str) -> bool:
    return send_message(
        bot_token,
        chat_id,
        "✅ <b>StockNotifier KlikIndomaret</b> terhubung!\nNotifikasi stok akan dikirim ke chat ini.",
    )
