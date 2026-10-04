"""
Monitor stok: bandingkan kondisi produk saat ini vs kondisi terakhir
(state.json), lalu bangun notifikasi Telegram untuk setiap perubahan.

Aturan notifikasi (dapat diatur lewat config "notify_on"):
  - in_stock    : produk yang tadinya habis/tidak ada kini TERSEDIA (barang masuk)
  - out_of_stock: produk yang tadinya ada kini habis/hilang dari katalog
  - price_drop  : harga final turun
  - discount    : muncul diskon baru (discountText berubah dari null/0)
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from klikidm import KlikIndomaretClient, Product, fetch_product
from telegram_notify import escape_html, send_message

log = logging.getLogger("monitor")

STATE_FILE = Path(__file__).parent / "state.json"

# status internal yang dinormalisasi: "available" | "unavailable" | "unknown"


def normalize_status(p: Optional[Product]) -> str:
    if p is None:
        return "unavailable"  # hilang dari hasil pencarian = tidak bisa dibeli
    if p.selling is False:
        return "unavailable"
    if p.selling is True:
        return "available"
    return "unknown"


def load_state() -> Dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            log.warning("state.json rusak, mulai dari kosong")
    return {"products": {}}


def save_state(state: Dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def format_message(kind: str, product: Product, prev: Dict[str, Any], cfg: Dict[str, Any]) -> str:
    name = escape_html(product.name)
    url = product.url or f"https://www.klikindomaret.com/xpress/{product.permalink}"
    price = f"Rp {product.final_price:,.0f}".replace(",", ".") if product.final_price else "-"
    store = cfg.get("store", {}).get("storeCode", "?")
    mode = cfg.get("store", {}).get("mode", "DELIVERY")

    if kind == "in_stock":
        header = "🟢 <b>BARANG MASUK!</b>"
    elif kind == "out_of_stock":
        header = "🔴 <b>STOK HABIS</b>"
    elif kind == "price_drop":
        header = "💰 <b>HARGA TURUN!</b>"
    else:
        header = "🏷️ <b>DISKON BARU!</b>"

    lines = [
        header,
        "",
        f"📦 <b>{name}</b>",
        f"💵 Harga: <b>{price}</b>",
    ]
    if product.discount_text and product.discount_text not in ("", "0%", None):
        lines.append(f"🏷️ Diskon: {product.discount_text}")
    if kind == "price_drop" and prev.get("final_price"):
        old = f"Rp {prev['final_price']:,.0f}".replace(",", ".")
        lines.append(f"⬇️ Sebelumnya: {old}")
    lines += [
        "",
        f"🏪 Toko: {store} ({mode})",
        f"🔗 {url}",
        f"🔢 PLU: <code>{product.plu}</code>",
    ]
    return "\n".join(lines)


def check_once(cfg: Dict[str, Any], client: KlikIndomaretClient, tg: Dict[str, str]) -> List[str]:
    """Satu siklus pengecekan. Return daftar ringkasan (untuk console)."""
    state = load_state()
    products_cfg = cfg.get("products", [])
    notify_on = set(cfg.get("notify_on", ["in_stock", "out_of_stock", "price_drop", "discount"]))
    summaries: List[str] = []

    if not products_cfg:
        log.warning("Watchlist kosong. Tambahkan produk: python main.py add <PLU / URL / kata kunci>")
        return summaries

    for item in products_cfg:
        plu = str(item.get("plu", "")).strip()
        permalink = str(item.get("permalink", "")).strip()
        display_name = item.get("name") or permalink or plu

        current: Optional[Product] = None
        try:
            current = fetch_product(client, item)
        except Exception as e:
            summaries.append(f"⚠️ {display_name}: {e}")
            # jangan update state saat gagal -> tidak akan false alert
            continue

        prev = state["products"].get(plu or permalink, {})
        now_status = normalize_status(current)
        prev_status = prev.get("status", "unknown")
        name = (current.name if current else None) or display_name

        messages: List[str] = []
        became_in_stock = prev_status == "unavailable" and now_status == "available"
        became_out = prev_status == "available" and now_status == "unavailable"
        price_drop = (
            prev_status != "unknown"
            and current is not None
            and prev.get("final_price") is not None
            and current.final_price is not None
            and current.final_price < prev["final_price"]
        )
        new_discount = bool(
            prev_status != "unknown"
            and current
            and current.discount_text
            and not prev.get("discount_text")
            and now_status == "available"
        )

        if became_in_stock and "in_stock" in notify_on:
            messages.append(format_message("in_stock", current, prev, cfg))
        elif became_out and "out_of_stock" in notify_on:
            messages.append(format_message("out_of_stock", current, prev, cfg))
        if price_drop and not became_in_stock and "price_drop" in notify_on:
            messages.append(format_message("price_drop", current, prev, cfg))
        if new_discount and not price_drop and "discount" in notify_on:
            messages.append(format_message("discount", current, prev, cfg))

        # simpan kondisi terbaru — hanya field yang berubah maknanya,
        # agar state.json stabil antar-run (tidak bikin commit tiap siklus)
        state["products"][plu or permalink] = {
            "status": now_status,
            "price": current.price if current else prev.get("price"),
            "final_price": current.final_price if current else prev.get("final_price"),
            "discount_text": current.discount_text if current else prev.get("discount_text"),
            "name": name,
            "selling": current.selling if current else None,
        }
        save_state(state)

        if current is None:
            summaries.append(f"❔ {name}: tidak ditemukan di katalog")
        else:
            harga = f"Rp {current.final_price:,.0f}".replace(",", ".") if current.final_price else "-"
            summaries.append(f"{'✅' if now_status == 'available' else '❌'} {name} — {harga}")

        for msg in messages:
            print("\n" + re.sub(r"<[^>]+>", "", msg))
            if tg.get("bot_token") and tg.get("chat_id"):
                sent = send_message(tg["bot_token"], tg["chat_id"], msg)
                if not sent:
                    summaries.append("   (gagal kirim ke Telegram — cek token/chat_id)")
    return summaries


def watch(cfg: Dict[str, Any], client: KlikIndomaretClient, tg: Dict[str, str]) -> None:
    interval = float(cfg.get("polling", {}).get("interval_seconds", 300))
    jitter = float(cfg.get("polling", {}).get("jitter_seconds", 60))
    consecutive_fail = 0
    while True:
        try:
            summaries = check_once(cfg, client, tg)
            for s in summaries:
                print(s)
            consecutive_fail = sum(1 for s in summaries if s.startswith("⚠️"))
        except KeyboardInterrupt:
            raise
        except Exception as e:
            consecutive_fail += 1
            log.error("Siklus gagal: %s", e)

        # backoff sederhana saat API mulai menolak
        sleep = interval + random.uniform(0, jitter)
        if consecutive_fail >= 3:
            sleep *= 2
            log.info("Banyak kegagalan berturut-turut, backoff jadi %.0f detik", sleep)
        log.info("Cek berikutnya dalam %.0f detik...", sleep)
        time.sleep(sleep)
