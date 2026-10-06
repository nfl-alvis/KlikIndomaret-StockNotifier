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


def get_updates(bot_token: str, offset: int) -> list | None:
    """
    Ambil pesan baru untuk bot (polling pendek, cocok untuk siklus CI).
    Return list update, list kosong, atau None saat error
    (mis. rate limit / webhook aktif).
    """
    url = f"{TELEGRAM_API}/bot{bot_token}/getUpdates"
    try:
        resp = requests.get(
            url,
            params={"offset": offset, "timeout": 0, "allowed_updates": '["message"]'},
            timeout=20,
        )
    except requests.RequestException as e:
        log.error("getUpdates gagal: %s", e)
        return None
    if resp.status_code != 200:
        log.error("getUpdates HTTP %s: %s", resp.status_code, resp.text[:200])
        return None
    body = resp.json()
    if body.get("ok") is not True:
        log.error("getUpdates ditolak: %s", resp.text[:200])
        return None
    return body.get("result") or []
