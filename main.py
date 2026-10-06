"""
StockNotifier KlikIndomaret — entry point CLI.

Perintah:
    python main.py run                   # mode CI: proses perintah bot + cek stok + exit code
    python main.py watch                 # jalankan monitor terus-menerus (lokal)
    python main.py check                 # sekali cek semua produk (tanpa bot)
    python main.py add <PLU|URL|kata>    # tambah produk ke watchlist
    python main.py list                  # lihat watchlist
    python main.py remove <PLU>          # hapus dari watchlist
    python main.py test-telegram         # tes koneksi bot Telegram
    python main.py stores                # daftar toko (storeCode) di area Anda
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

from appconfig import CONFIG_FILE, load_config, save_config
from klikidm import KlikIndomaretClient, Product, fetch_product, parse_query
from monitor import KNOWN_NOTIFY, check_once, watch
from telegram_notify import test as tg_test

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("main")


def validate_config(cfg: dict) -> list[str]:
    """Return daftar masalah konfigurasi (kosong = valid)."""
    problems: list[str] = []
    polling = cfg.get("polling") or {}
    for key in ("interval_seconds", "jitter_seconds", "request_delay_seconds"):
        v = polling.get(key)
        if not isinstance(v, (int, float)) or v <= 0:
            problems.append(f"polling.{key} harus angka > 0 (sekarang: {v!r})")
    store = cfg.get("store") or {}
    if not store.get("storeCode"):
        problems.append("store.storeCode wajib diisi (mis. TMLG)")
    if not store.get("districtId"):
        problems.append("store.districtId wajib diisi")
    notify = cfg.get("notify_on")
    if not isinstance(notify, list) or not notify:
        problems.append("notify_on harus list berisi minimal satu jenis")
    else:
        for n in notify:
            if n not in KNOWN_NOTIFY:
                problems.append(f"notify_on '{n}' tidak dikenal (pilihan: {', '.join(sorted(KNOWN_NOTIFY))})")
    if not isinstance(cfg.get("products"), list):
        problems.append("products harus berupa list")
    else:
        for i, p in enumerate(cfg["products"]):
            if not (str(p.get("plu") or "").strip() or str(p.get("permalink") or "").strip()):
                problems.append(f"products[{i}] butuh 'plu' atau 'permalink'")
    watch = cfg.get("new_product_watch") or {}
    if watch.get("enabled") and not watch.get("keywords"):
        problems.append("new_product_watch.enabled tapi keywords kosong")
    if cfg.get("pat_expiry"):
        try:
            datetime.strptime(str(cfg["pat_expiry"]), "%Y-%m-%d")
        except ValueError:
            problems.append(f"pat_expiry '{cfg['pat_expiry']}' bukan format YYYY-MM-DD")
    return problems


def tg_conf(cfg: dict) -> dict:
    """Token dari env var dulu (dipakai GitHub Actions), lalu config.json."""
    t = cfg.get("telegram", {})
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or t.get("bot_token", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or str(t.get("chat_id", ""))
    if not token or not chat:
        return {}
    return {"bot_token": token, "chat_id": chat}


def make_client(cfg: dict) -> KlikIndomaretClient:
    store = cfg.get("store", {})
    return KlikIndomaretClient(
        store_code=store.get("storeCode", "TJKT"),
        district_id=str(store.get("districtId", "141100100")),
        latitude=store.get("latitude", -6.1763897),
        longitude=store.get("longitude", 106.82667),
        mode=store.get("mode", "DELIVERY"),
        request_delay=float(cfg.get("polling", {}).get("request_delay_seconds", 3.0)),
    )


def cmd_add(cfg: dict, query: str, index: int, no_save: bool) -> None:
    client = make_client(cfg)
    target = parse_query(query)
    products: list[Product] = []

    if "permalink" in target:
        # URL produk: langsung ke endpoint detail (berbasis permalink)
        p = fetch_product(client, {"permalink": target["permalink"]})
        products = [p] if p else []
        if not products:  # fallback: cari dengan kata-kata di slug
            products = client.search(keyword=target["permalink"].replace("-", " "), size=10)
    elif "plu" in target:
        p = client.find_by_plu(target["plu"])
        products = [p] if p else []
    else:
        products = client.search(keyword=target["keyword"], size=10)

    if not products:
        print("Tidak ada produk ditemukan. Coba kata kunci lain.")
        sys.exit(1)

    for i, p in enumerate(products[:10]):
        harga = f"Rp {p.final_price:,.0f}".replace(",", ".") if p.final_price else "-"
        status = "✅ tersedia" if p.selling else "❌ tidak dijual"
        print(f"[{i}] {p.name}\n    PLU {p.plu} | {harga} | {status}\n    {p.url}")

    chosen = products[index] if index < len(products) else products[0]

    # lengkapi data dari detail produk bila tersedia (opsional)
    detail = client.get_detail(chosen.permalink) if chosen.permalink else None
    if detail:
        d = detail.get("product") or detail
        if not chosen.name and d.get("productName"):
            chosen.name = d["productName"]

    print(f"\nDipilih: {chosen.name} (PLU {chosen.plu})")
    if no_save:
        return

    cfg.setdefault("products", [])
    if any(str(x.get("plu")) == chosen.plu for x in cfg["products"]):
        print("Produk sudah ada di watchlist.")
        return
    cfg["products"].append(
        {
            "plu": chosen.plu,
            "permalink": chosen.permalink,
            "name": chosen.name,
            "url": chosen.url,
        }
    )
    save_config(cfg)
    print(f"Disimpan ke config.json. Total watchlist: {len(cfg['products'])} produk.")


def cmd_list(cfg: dict) -> None:
    products = cfg.get("products", [])
    if not products:
        print("Watchlist kosong. Tambah dengan: python main.py add <PLU|URL|kata kunci>")
        return
    print(f"Watchlist ({len(products)} produk):")
    for p in products:
        print(f"  - PLU {p.get('plu')} | {p.get('name')}")
        print(f"    {p.get('url')}")


def cmd_remove(cfg: dict, plu: str) -> None:
    before = len(cfg.get("products", []))
    cfg["products"] = [p for p in cfg.get("products", []) if str(p.get("plu")) != plu]
    save_config(cfg)
    print(f"Dihapus {before - len(cfg['products'])} produk.")


def cmd_stores(cfg: dict) -> None:
    client = make_client(cfg)
    for s in client.nearby_stores():
        print(f"storeCode={s.get('storeCode')} | {s.get('storeName')} | area={s.get('areaName')}")


def cmd_run(cfg: dict) -> None:
    """Mode CI: proses perintah bot -> cek stok -> heartbeat dsb -> exit code."""
    client = make_client(cfg)
    tg = tg_conf(cfg)
    check_requested = False

    if tg:
        from bot import process_updates

        check_requested, cfg = process_updates(cfg, client, tg)
    else:
        print("⚠️  Telegram belum dikonfigurasi — notifikasi hanya tampil di console.")

    summaries = check_once(cfg, client, tg)
    for s in summaries:
        print(s)

    if check_requested and tg:
        from telegram_notify import send_message

        teks = "📋 <b>Hasil cek stok:</b>\n" + "\n".join(escape_html(s) for s in summaries) or "tidak ada data"
        send_message(tg["bot_token"], tg["chat_id"], teks)

    failures = [s for s in summaries if s.startswith("⚠️")]
    products = cfg.get("products", [])
    if products and len(failures) == len(products):
        log.error("SEMUA produk gagal dicek — menandakan anti-bot/gangguan. Exit 2 agar GitHub mengirim email.")
        sys.exit(2)


def main() -> None:
    parser = argparse.ArgumentParser(description="StockNotifier KlikIndomaret")
    parser.add_argument(
        "command", nargs="?", default="watch",
        choices=["run", "watch", "check", "add", "list", "remove", "test-telegram", "stores"],
    )
    parser.add_argument("query", nargs="?", help="PLU / URL produk / kata kunci (untuk add|remove)")
    parser.add_argument("--index", type=int, default=0, help="pilih nomor hasil saat add (default 0)")
    parser.add_argument("--no-save", action="store_true", help="add: hanya tampilkan, jangan simpan")
    args = parser.parse_args()

    cfg = load_config()
    problems = validate_config(cfg)
    if problems:
        print("❌ Konfigurasi tidak valid:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)

    if args.command == "test-telegram":
        tg = tg_conf(cfg)
        if not tg:
            print("Isi telegram.bot_token & telegram.chat_id di config.json dulu (atau set env TELEGRAM_BOT_TOKEN & TELEGRAM_CHAT_ID).")
            sys.exit(1)
        if tg_test(tg["bot_token"], tg["chat_id"]):
            print("Berhasil!")
        else:
            print("Gagal — cek token/chat_id.")
            sys.exit(1)
        return

    if args.command == "run":
        cmd_run(cfg)
    elif args.command == "watch":
        client = make_client(cfg)
        tg = tg_conf(cfg)
        if not tg:
            print("⚠️  Telegram belum dikonfigurasi — notifikasi hanya tampil di console.")
        print(f"Monitor berjalan. Watchlist: {len(cfg.get('products', []))} produk. Ctrl+C untuk berhenti.")
        try:
            watch(cfg, client, tg)
        except KeyboardInterrupt:
            print("\nDihentikan.")
    elif args.command == "check":
        client = make_client(cfg)
        for line in check_once(cfg, client, tg_conf(cfg)):
            print(line)
    elif args.command == "add":
        if not args.query:
            print("Pemakaian: python main.py add <PLU|URL|kata kunci>")
            sys.exit(1)
        cmd_add(cfg, args.query, args.index, args.no_save)
    elif args.command == "list":
        cmd_list(cfg)
    elif args.command == "remove":
        if not args.query:
            print("Pemakaian: python main.py remove <PLU>")
            sys.exit(1)
        cmd_remove(cfg, args.query)
    elif args.command == "stores":
        cmd_stores(cfg)


if __name__ == "__main__":
    main()
