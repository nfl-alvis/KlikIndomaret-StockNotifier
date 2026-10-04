"""
StockNotifier KlikIndomaret — entry point CLI.

Perintah:
    python main.py watch                  # jalankan monitor terus-menerus (default)
    python main.py check                  # sekali cek semua produk di watchlist
    python main.py add <PLU|URL|kata>     # tambah produk ke watchlist
    python main.py list                   # lihat watchlist
    python main.py remove <PLU>           # hapus dari watchlist
    python main.py test-telegram          # tes koneksi bot Telegram
    python main.py stores                 # daftar toko (storeCode) di area Anda
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path

from klikidm import KlikIndomaretClient, Product, fetch_product
from monitor import check_once, watch
from telegram_notify import test as tg_test

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        example = BASE_DIR / "config.example.json"
        if example.exists():
            CONFIG_FILE.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"config.json belum ada — dibuat salinan dari config.example.json.\n"
                  f"Isi telegram.bot_token & telegram.chat_id di {CONFIG_FILE}, lalu jalankan lagi.")
            sys.exit(1)
        print("config.json tidak ditemukan!")
        sys.exit(1)
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


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


def tg_conf(cfg: dict) -> dict:
    """Token dari env var dulu (dipakai GitHub Actions), lalu config.json."""
    t = cfg.get("telegram", {})
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or t.get("bot_token", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or str(t.get("chat_id", ""))
    if not token or not chat:
        return {}
    return {"bot_token": token, "chat_id": chat}


def parse_query(q: str) -> dict:
    """Kenali bentuk input: URL produk, PLU angka, atau kata kunci."""
    m = re.search(r"klikindomaret\.com/xpress/([^/?]+)", q)
    if m:
        return {"permalink": m.group(1)}
    if q.isdigit() and len(q) >= 6:
        return {"plu": q}
    return {"keyword": q}


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


def main() -> None:
    parser = argparse.ArgumentParser(description="StockNotifier KlikIndomaret")
    parser.add_argument("command", nargs="?", default="watch",
                        choices=["watch", "check", "add", "list", "remove", "test-telegram", "stores"])
    parser.add_argument("query", nargs="?", help="PLU / URL produk / kata kunci (untuk add|remove)")
    parser.add_argument("--index", type=int, default=0, help="pilih nomor hasil saat add (default 0)")
    parser.add_argument("--no-save", action="store_true", help="add: hanya tampilkan, jangan simpan")
    args = parser.parse_args()

    if args.command == "test-telegram":
        cfg = load_config()
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

    cfg = load_config()

    if args.command == "watch":
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
