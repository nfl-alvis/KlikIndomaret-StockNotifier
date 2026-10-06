"""
Bot Telegram: perintah diproses di awal tiap siklus CI (latensi jawaban
<= interval polling, tanpa server tambahan). Hanya chat_id pemilik yang
dilayani — pesan dari orang lain diabaikan.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from appconfig import load_config, save_config
from klikidm import KlikIndomaretClient, Product, fetch_product, parse_query
from monitor import load_state, save_state
from telegram_notify import escape_html, get_updates, send_message

log = logging.getLogger("bot")

MAX_WATCHLIST = 60  # kebijakan skala sopan (lihat README)

HELP = (
    "🤖 <b>StockNotifier Bot</b>\n\n"
    "/status — kondisi terakhir watchlist\n"
    "/list — daftar produk dipantau\n"
    "/check — cek stok sekarang\n"
    "/add &lt;PLU | URL | kata kunci&gt; — tambah produk\n"
    "/remove &lt;PLU&gt; — hapus produk\n"
    "/help — bantuan\n\n"
    "Balasan bisa molor sampai 5 menit (bot hidup di GitHub Actions)."
)


def _harga(p: Optional[Product]) -> str:
    return f"Rp {p.final_price:,.0f}".replace(",", ".") if p.final_price else "-"


def _resolve(client: KlikIndomaretClient, target: Dict[str, str]) -> Optional[Product]:
    if "plu" in target:
        return client.find_by_plu(target["plu"])
    if "permalink" in target:
        p = fetch_product(client, {"permalink": target["permalink"]})
        if p:
            return p
        found = client.search(keyword=target["permalink"].replace("-", " "), size=10)
        return next((x for x in found if x.permalink == target["permalink"]), None)
    found = client.search(keyword=target["keyword"], size=10)
    return next((x for x in found if x.selling), None) or (found[0] if found else None)


def _cmd_status(cfg: Dict[str, Any]) -> str:
    state = load_state()
    entries = state.get("products") or {}
    if not cfg.get("products"):
        return "Watchlist kosong. Tambah dengan /add <kata kunci>."
    lines = ["📋 <b>Kondisi terakhir watchlist:</b>"]
    for item in cfg["products"]:
        st = entries.get(str(item.get("plu")) or item.get("permalink"), {})
        emoji = {"available": "✅", "unavailable": "❌"}.get(st.get("status", "unknown"), "❔")
        harga = st.get("final_price")
        baris = f"{emoji} {escape_html(str(item.get('name')))}"
        if harga:
            baris += f" — Rp {harga:,.0f}".replace(",", ".")
        if isinstance(st.get("qty_struk"), int) and st["qty_struk"] > 0:
            baris += f" | alokasi {st['qty_struk']}"
        lines.append(baris)
    return "\n".join(lines)


def _cmd_add(cfg: Dict[str, Any], client: KlikIndomaretClient, arg: str) -> Tuple[str, bool]:
    if not arg:
        return "Pemakaian: /add <PLU | URL | kata kunci>", False
    if len(cfg.get("products", [])) >= MAX_WATCHLIST:
        return f"Watchlist penuh (maks {MAX_WATCHLIST}) — hapus dulu dengan /remove.", False
    target = parse_query(arg.strip())
    p = _resolve(client, target)
    if not p:
        return f"Tidak ada produk cocok untuk '<b>{escape_html(arg)}</b>'. Coba kata kunci lain.", False
    if any(str(x.get("plu")) == p.plu for x in cfg.get("products", [])):
        return f"Sudah dipantau: {escape_html(p.name)} (PLU {p.plu})", False
    cfg.setdefault("products", []).append(
        {"plu": p.plu, "permalink": p.permalink, "name": p.name, "url": p.url}
    )
    save_config(cfg)
    return (
        f"✅ Ditambahkan: <b>{escape_html(p.name)}</b>\n"
        f"🔢 PLU {p.plu} | {_harga(p)}\n{p.url}\n\n"
        "Pantauan mulai siklus berikutnya (baseline dulu, notif menyusul). "
        "Kalau ini varian yang salah, /remove lalu /add dengan PLU persis.",
        True,
    )


def _cmd_remove(cfg: Dict[str, Any], arg: str) -> Tuple[str, bool]:
    if not arg:
        return "Pemakaian: /remove <PLU>", False
    plu = arg.strip()
    before = len(cfg.get("products", []))
    cfg["products"] = [x for x in cfg.get("products", []) if str(x.get("plu")) != plu]
    if len(cfg["products"]) == before:
        return f"PLU {escape_html(plu)} tidak ada di watchlist.", False
    save_config(cfg)
    return f"🗑️ PLU {escape_html(plu)} dihapus. Sisa {len(cfg['products'])} produk.", True


def process_updates(
    cfg: Dict[str, Any], client: KlikIndomaretClient, tg: Dict[str, str]
) -> Tuple[bool, Dict[str, Any]]:
    """
    Proses pesan baru. Return (check_requested, cfg_terbaru).
    cfg di-reload bila bot mengubahnya (add/remove).
    """
    if not tg:
        return False, cfg
    state = load_state()
    last_id = int(state.get("bot", {}).get("last_update_id", 0))
    updates = get_updates(tg["bot_token"], offset=last_id + 1)
    if updates is None:
        return False, cfg

    check_requested = False
    changed = False
    max_id = last_id
    for u in updates:
        max_id = max(max_id, u.get("update_id", max_id))
        msg = u.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        if chat_id != str(tg["chat_id"]):
            log.warning("Pesan diabaikan dari chat asing %s", chat_id)
            continue

        parts = text.split(maxsplit=1)
        cmd = parts[0].lower().split("@")[0]
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("/start", "/help"):
            send_message(tg["bot_token"], chat_id, HELP)
        elif cmd == "/status":
            send_message(tg["bot_token"], chat_id, _cmd_status(cfg))
        elif cmd == "/list":
            if not cfg.get("products"):
                send_message(tg["bot_token"], chat_id, "Watchlist kosong.")
            else:
                lines = [f"📋 Watchlist ({len(cfg['products'])}):"]
                for x in cfg["products"]:
                    lines.append(f"• PLU {x.get('plu')} — {escape_html(str(x.get('name')))}")
                send_message(tg["bot_token"], chat_id, "\n".join(lines))
        elif cmd == "/check":
            check_requested = True
            send_message(tg["bot_token"], chat_id, "⏳ Oke, memeriksa stok sekarang… hasil menyusul.")
        elif cmd == "/add":
            reply, did = _cmd_add(cfg, client, arg)
            send_message(tg["bot_token"], chat_id, reply)
            changed = changed or did
        elif cmd == "/remove":
            reply, did = _cmd_remove(cfg, arg)
            send_message(tg["bot_token"], chat_id, reply)
            changed = changed or did
        elif cmd.startswith("/"):
            send_message(tg["bot_token"], chat_id, "Perintah tidak dikenal.\n\n" + HELP)

    if max_id != last_id:
        state.setdefault("bot", {})["last_update_id"] = max_id
        save_state(state)
    if changed:
        cfg = load_config(quiet=True)
    return check_requested, cfg
