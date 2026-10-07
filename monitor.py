"""
Monitor stok: bandingkan kondisi produk saat ini vs kondisi terakhir
(state.json), kirim notifikasi Telegram, plus fitur operasional:

  - deteksi transisi stok/harga/diskon/alokasi (detect_changes, murni & teruji)
  - antrean notifikasi tertunda saat pengiriman gagal (missed alerts)
  - pengingat kedaluwarsa PAT (bot mati diam-diam kalau token expired)
  - heartbeat harian supaya "diam" tidak ambigu
  - deteksi produk baru per kata kunci (new_product_watch)
  - riwayat harga ringkas per produk
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from klikidm import KlikIndomaretClient, Product, fetch_product
from telegram_notify import escape_html, send_message

log = logging.getLogger("monitor")

STATE_FILE = Path(__file__).parent / "state.json"
WIB = timezone(timedelta(hours=7))
KNOWN_NOTIFY = {"in_stock", "out_of_stock", "price_drop", "price_increase", "discount", "qty_drop"}
MAX_MISSED = 20
MAX_HISTORY = 30
MAX_NEW_PRODUCT_ALERTS = 5


def load_state() -> Dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            log.warning("state.json rusak, mulai dari kosong")
    return {"products": {}}


def save_state(state: Dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_status(p: Optional[Product]) -> str:
    if p is None:
        return "unavailable"  # hilang dari hasil pencarian = tidak bisa dibeli
    if p.selling is False:
        return "unavailable"
    if p.selling is True:
        return "available"
    return "unknown"


def detect_changes(
    prev: Dict[str, Any], current: Optional[Product], notify_on: set
) -> List[Tuple[str, Optional[Product]]]:
    """
    Murni (tanpa I/O): bandingkan kondisi sebelumnya vs sekarang,
    return daftar event notifikasi [(kind, current)].
    Siklus pertama (prev kosong) sengaja senyap = baseline.
    """
    events: List[Tuple[str, Optional[Product]]] = []
    prev_status = prev.get("status", "unknown")
    now_status = normalize_status(current)

    became_in = prev_status == "unavailable" and now_status == "available"
    became_out = prev_status == "available" and now_status == "unavailable"

    if became_in and "in_stock" in notify_on:
        events.append(("in_stock", current))
    if became_out and "out_of_stock" in notify_on:
        events.append(("out_of_stock", None))

    if current is not None and prev_status != "unknown":
        prev_price = prev.get("final_price")
        cur_price = current.final_price
        if prev_price is not None and cur_price is not None and cur_price != prev_price:
            if cur_price < prev_price and "price_drop" in notify_on and not became_in:
                events.append(("price_drop", current))
            elif cur_price > prev_price and "price_increase" in notify_on and not became_in:
                events.append(("price_increase", current))

        if (
            current.discount_text
            and not prev.get("discount_text")
            and now_status == "available"
            and "discount" in notify_on
            and not became_in
        ):
            events.append(("discount", current))

        qty_now = current.qty_struk
        qty_prev = prev.get("qty_struk")
        if (
            now_status == "available"
            and qty_now is not None
            and (qty_prev is None or qty_now < qty_prev)
            and "qty_drop" in notify_on
            and not became_in
        ):
            events.append(("qty_drop", current))

    return events


def format_message(
    kind: str,
    product: Optional[Product],
    prev: Dict[str, Any],
    cfg: Dict[str, Any],
    item: Dict[str, Any],
) -> str:
    prev_name = prev.get("name") or item.get("name") or "?"
    name = escape_html((product.name if product else None) or prev_name)
    url = (
        (product.url if product else None)
        or item.get("url")
        or f"https://www.klikindomaret.com/xpress/{item.get('permalink', '')}"
    )
    price_val = product.final_price if product else prev.get("final_price")
    price = f"Rp {price_val:,.0f}".replace(",", ".") if price_val else "-"
    store = cfg.get("store", {}).get("storeCode", "?")
    mode = cfg.get("store", {}).get("mode", "DELIVERY")

    headers = {
        "in_stock": "🟢 <b>BARANG MASUK!</b>",
        "out_of_stock": "🔴 <b>STOK HABIS</b>",
        "price_drop": "💰 <b>HARGA TURUN!</b>",
        "price_increase": "📈 <b>HARGA NAIK</b>",
        "qty_drop": (
            "🆕 <b>ALOKASI TERBATAS MUNCUL!</b>"
            if prev.get("qty_struk") is None
            else "📉 <b>STOK MENIPIS!</b>"
        ),
        "discount": "🏷️ <b>DISKON BARU!</b>",
    }
    lines = [headers.get(kind, "🔔 <b>UPDATE PRODUK</b>"), "", f"📦 <b>{name}</b>"]

    if product is not None:
        lines.append(f"💵 Harga: <b>{price}</b>")
        if product.discount_text and product.discount_text not in ("", "0%", None):
            lines.append(f"🏷️ Diskon: {product.discount_text}")
    else:
        lines.append(f"💵 Harga terakhir: {price}")

    if kind == "price_drop" and prev.get("final_price"):
        old = f"Rp {prev['final_price']:,.0f}".replace(",", ".")
        lines.append(f"⬇️ Sebelumnya: {old}")
    if kind == "price_increase" and prev.get("final_price"):
        old = f"Rp {prev['final_price']:,.0f}".replace(",", ".")
        lines.append(f"⬆️ Sebelumnya: {old}")
    if kind == "qty_drop" and product is not None and product.qty_struk is not None:
        if prev.get("qty_struk") is None:
            lines.append(f"📦 Sisa alokasi pickup: <b>{product.qty_struk}</b>")
        else:
            lines.append(
                f"📦 Sisa alokasi pickup: <b>{product.qty_struk}</b> (sebelumnya {prev['qty_struk']})"
            )
    if kind == "in_stock" and product is not None and product.qty_struk is not None:
        lines.append(f"📦 Sisa alokasi pickup: <b>{product.qty_struk}</b>")

    lines += [
        "",
        f"🏪 Kanal: XPRESS {store} ({mode}) — harga & stok kanal Xpress, bukan harga toko retail",
        f"🔗 {url}",
        f"🔢 PLU: <code>{item.get('plu', '?')}</code>",
    ]
    return "\n".join(lines)


# ----------------------------------------------------------------- operasional


def warn_pat_expiry(cfg: Dict[str, Any], tg: Dict[str, str], state: Dict[str, Any]) -> None:
    """Pengingat kedaluwarsa PAT cron-job.org (H-7 atau sudah lewat, 1x/hari)."""
    expiry_raw = str(cfg.get("pat_expiry") or "").strip()
    if not expiry_raw or not tg:
        return
    try:
        expiry = datetime.strptime(expiry_raw, "%Y-%m-%d").date()
    except ValueError:
        log.warning("pat_expiry '%s' bukan format YYYY-MM-DD — dilewati", expiry_raw)
        return
    today = datetime.now(WIB).date()
    days_left = (expiry - today).days
    if days_left > 7:
        return
    today_iso = today.isoformat()
    if state.get("last_pat_warn") == today_iso:
        return
    if days_left < 0:
        msg = f"🚨 <b>PAT GitHub SUDAH KEDALUWARSA sejak {expiry}</b> — cron-job.org gagal 401 dan notifikasi MATI. Perbarui token sekarang!"
    elif days_left <= 3:
        msg = f"⏰ <b>PAT GitHub kedaluwarsa {expiry} (H-{days_left})!</b> Perbarui sebelum notifikasi berhenti diam-diam."
    else:
        msg = f"⏰ Pengingat: PAT GitHub kedaluwarsa {expiry} (H-{days_left}). Perbarui sebelum lewat."
    if send_message(tg["bot_token"], tg["chat_id"], msg):
        state["last_pat_warn"] = today_iso
        save_state(state)


def flush_missed(state: Dict[str, Any], tg: Dict[str, str]) -> None:
    """Kirim ulang notifikasi yang gagal terkirim di siklus sebelumnya."""
    missed = state.get("missed") or []
    if not missed or not tg:
        return
    sisa = []
    for m in missed:
        if not send_message(tg["bot_token"], tg["chat_id"], "⏮️ (terlambat)\n" + m):
            sisa.append(m)
    if sisa:
        state["missed"] = sisa
        save_state(state)
    else:
        state.pop("missed", None)
        save_state(state)


def queue_missed(state: Dict[str, Any], msg: str) -> None:
    missed = state.get("missed") or []
    if len(missed) < MAX_MISSED:
        missed.append(msg)
    state["missed"] = missed


def send_alert(state: Dict[str, Any], tg: Dict[str, str], msg: str) -> None:
    """Kirim alert; kalau gagal, masuk antrean agar tidak hilang."""
    if not tg:
        print(re.sub(r"<[^>]+>", "", msg))
        return
    if not send_message(tg["bot_token"], tg["chat_id"], msg):
        queue_missed(state, msg)


def check_new_products(cfg: Dict[str, Any], client: KlikIndomaretClient, tg: Dict[str, str], state: Dict[str, Any]) -> None:
    """Deteksi produk baru pada kata kunci yang dipantau (new_product_watch)."""
    watch = cfg.get("new_product_watch") or {}
    if not watch.get("enabled") or not watch.get("keywords") or not tg:
        return
    known = state.setdefault("new_watch", {})
    for kw in watch["keywords"]:
        try:
            products = client.search(keyword=str(kw), size=20)
        except Exception as e:
            log.warning("new_product_watch '%s' gagal: %s", kw, e)
            continue
        plus = [p.plu for p in products]
        prev_plus = set(known.get(kw) or [])
        if not prev_plus:
            known[kw] = plus  # baseline pertama
            save_state(state)
            continue
        baru = [p for p in products if p.plu not in prev_plus]
        if baru:
            lines = [f"🆕 <b>Produk baru di '{escape_html(str(kw))}':</b>"]
            for p in baru[:MAX_NEW_PRODUCT_ALERTS]:
                harga = f"Rp {p.final_price:,.0f}".replace(",", ".") if p.final_price else "-"
                lines.append(f"• {escape_html(p.name)} — {harga}")
            if len(baru) > MAX_NEW_PRODUCT_ALERTS:
                lines.append(f"…dan {len(baru) - MAX_NEW_PRODUCT_ALERTS} lainnya")
            send_alert(state, tg, "\n".join(lines))
        known[kw] = plus
        save_state(state)


def maybe_heartbeat(cfg: Dict[str, Any], tg: Dict[str, str], state: Dict[str, Any]) -> None:
    """Ringkasan harian: bukti hidup + rekap status watchlist."""
    today = datetime.now(WIB).date().isoformat()
    if state.get("last_heartbeat") == today:
        return
    entries = state.get("products") or {}
    available = sum(1 for v in entries.values() if v.get("status") == "available")
    alokasi = [
        f"{escape_html((v.get('name') or '?')[:28])} ({v.get('qty_struk')})"
        for v in entries.values()
        if isinstance(v.get("qty_struk"), int) and v.get("qty_struk", 0) > 0
    ]
    lines = [
        f"📊 <b>Laporan harian</b> ({today})",
        f"✅ Tersedia: {available}/{len(entries)} produk dipantau",
    ]
    if alokasi:
        lines.append("🆕 Alokasi aktif: " + ", ".join(alokasi))
    lines.append("🔔 Monitor berjalan normal.")
    msg = "\n".join(lines)
    if tg:
        send_message(tg["bot_token"], tg["chat_id"], msg)
    else:
        print(re.sub(r"<[^>]+>", "", msg))
    state["last_heartbeat"] = today
    save_state(state)


# ------------------------------------------------------------------ siklus utama


def check_once(cfg: Dict[str, Any], client: KlikIndomaretClient, tg: Dict[str, str]) -> List[str]:
    """Satu siklus pengecekan. Return daftar ringkasan (untuk console)."""
    state = load_state()
    products_cfg = cfg.get("products", [])
    notify_on = set(cfg.get("notify_on", KNOWN_NOTIFY)) & KNOWN_NOTIFY
    summaries: List[str] = []

    warn_pat_expiry(cfg, tg, state)
    flush_missed(state, tg)

    if not products_cfg:
        log.warning("Watchlist kosong. Tambahkan produk: python main.py add <PLU / URL / kata kunci>")
        return summaries

    n_delay = float(cfg.get("polling", {}).get("request_delay_seconds", 3.0))
    if len(products_cfg) * n_delay > 240:
        log.warning(
            "%d produk × %.0fs jeda ≈ melebihi 4 menit/run — pertimbangkan interval lebih jarang.",
            len(products_cfg), n_delay,
        )

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
        name = (current.name if current else None) or display_name
        now_status = normalize_status(current)

        for kind, cur in detect_changes(prev, current, notify_on):
            msg = format_message(kind, cur, prev, cfg, item)
            send_alert(state, tg, msg)

        # simpan kondisi terbaru (hanya field bermakna, agar state stabil antar-run)
        price_history = prev.get("price_history") or []
        if current is not None and current.final_price is not None:
            if not price_history or price_history[-1].get("p") != current.final_price:
                price_history.append(
                    {"d": datetime.now(WIB).date().isoformat(), "p": current.final_price}
                )
                price_history = price_history[-MAX_HISTORY:]

        state["products"][plu or permalink] = {
            "status": now_status,
            "price": current.price if current else prev.get("price"),
            "final_price": current.final_price if current else prev.get("final_price"),
            "discount_text": current.discount_text if current else prev.get("discount_text"),
            "qty_struk": current.qty_struk if current else prev.get("qty_struk"),
            "name": name,
            "selling": current.selling if current else None,
            "price_history": price_history,
        }
        save_state(state)

        if current is None:
            summaries.append(f"❔ {name}: tidak ditemukan di katalog")
        else:
            harga = f"Rp {current.final_price:,.0f}".replace(",", ".") if current.final_price else "-"
            summaries.append(f"{'✅' if now_status == 'available' else '❌'} {name} — {harga}")

    check_new_products(cfg, client, tg, state)
    maybe_heartbeat(cfg, tg, state)
    return summaries


def watch(cfg: Dict[str, Any], client: KlikIndomaretClient, tg: Dict[str, str]) -> None:
    interval = float(cfg.get("polling", {}).get("interval_seconds", 300))
    jitter = float(cfg.get("polling", {}).get("jitter_seconds", 60))
    delay = float(cfg.get("polling", {}).get("request_delay_seconds", 3.0))
    # auto-scale: interval minimum = produk × jeda × 2 (kebijakan sopan)
    minimum = len(cfg.get("products", [])) * delay * 2
    if minimum > interval:
        log.warning("Interval dinaikkan otomatis %.0f -> %.0f detik (%d produk × jeda %.0fs × 2).",
                    interval, minimum, len(cfg.get("products", [])), delay)
        interval = minimum
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
