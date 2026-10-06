"""Unit test logika monitor (murni, tanpa jaringan). Jalankan: python -m pytest -q"""

from klikidm import Product
from monitor import detect_changes, format_message, normalize_status

NOTIFY = {"in_stock", "out_of_stock", "price_drop", "price_increase", "discount", "qty_drop"}
ITEM = {
    "plu": "20016512",
    "permalink": "sunco-sunco-minyak-goreng-pch-2l",
    "name": "Sunco Minyak Goreng 2L",
    "url": "https://www.klikindomaret.com/xpress/sunco-sunco-minyak-goreng-pch-2l",
}
CFG = {"store": {"storeCode": "TMLG", "mode": "DELIVERY"}}


def make_product(selling=True, price=23000, qty=None, discount=None):
    return Product(
        plu="20016512",
        permalink="sunco-sunco-minyak-goreng-pch-2l",
        name="Sunco Minyak Goreng 2L",
        price=price,
        final_price=price,
        discount_text=discount,
        selling=selling,
        qty_struk=qty,
        url=ITEM["url"],
    )


def kinds(events):
    return [k for k, _ in events]


# ------------------------------------------------------------- normalisasi status

def test_normalize_status():
    assert normalize_status(None) == "unavailable"
    assert normalize_status(make_product(selling=True)) == "available"
    assert normalize_status(make_product(selling=False)) == "unavailable"


# ------------------------------------------------------------- baseline & transisi

def test_siklus_pertama_senyap():
    events = detect_changes({}, make_product(), NOTIFY)
    assert events == []  # baseline


def test_barang_masuk():
    prev = {"status": "unavailable", "final_price": 23000, "name": "Sunco"}
    events = detect_changes(prev, make_product(), NOTIFY)
    assert kinds(events) == ["in_stock"]


def test_stok_habis_menggunakan_prev():
    prev = {"status": "available", "final_price": 23000, "name": "Sunco", "qty_struk": 6}
    events = detect_changes(prev, None, NOTIFY)
    assert kinds(events) == ["out_of_stock"]


def test_harga_turun_dan_naik():
    prev = {"status": "available", "final_price": 23000, "name": "Sunco"}
    drop = detect_changes(prev, make_product(price=20000), NOTIFY)
    assert kinds(drop) == ["price_drop"]

    naik = detect_changes(prev, make_product(price=25000), NOTIFY)
    assert kinds(naik) == ["price_increase"]


def test_harga_sama_tidak_beri_notif():
    prev = {"status": "available", "final_price": 23000, "name": "Sunco"}
    assert detect_changes(prev, make_product(price=23000), NOTIFY) == []


def test_diskon_baru():
    prev = {"status": "available", "final_price": 23000, "name": "Sunco"}
    events = detect_changes(prev, make_product(discount="50%"), NOTIFY)
    assert kinds(events) == ["discount"]


# ------------------------------------------------------------- alokasi (qty)

def test_alokasi_muncul():
    prev = {"status": "available", "final_price": 23000, "name": "Sunco"}
    events = detect_changes(prev, make_product(qty=6), NOTIFY)
    assert kinds(events) == ["qty_drop"]


def test_alokasi_menipis():
    prev = {"status": "available", "final_price": 23000, "name": "Sunco", "qty_struk": 6}
    events = detect_changes(prev, make_product(qty=2), NOTIFY)
    assert kinds(events) == ["qty_drop"]


def test_alokasi_naik_tidak_beri_notif():
    prev = {"status": "available", "final_price": 23000, "name": "Sunco", "qty_struk": 2}
    assert detect_changes(prev, make_product(qty=6), NOTIFY) == []


def test_restock_dengan_alokasi_cuma_in_stock():
    # restock: satu pesan 🟢 yang memuat angka alokasi, bukan dua pesan
    prev = {"status": "unavailable", "final_price": 23000, "name": "Sunco"}
    events = detect_changes(prev, make_product(qty=6), NOTIFY)
    assert kinds(events) == ["in_stock"]


# ------------------------------------------------------------- filter notify_on

def test_notify_on_menyaring():
    prev = {"status": "unavailable", "final_price": 23000, "name": "Sunco"}
    events = detect_changes(prev, make_product(), {"price_drop"})
    assert events == []


# ------------------------------------------------------------- format pesan

def test_format_in_stock():
    prev = {"status": "unavailable", "final_price": 23000, "name": "Sunco"}
    msg = format_message("in_stock", make_product(qty=6), prev, CFG, ITEM)
    assert "BARANG MASUK" in msg
    assert "Sunco Minyak Goreng 2L" in msg
    assert "Rp 23.000" in msg
    assert "Sisa alokasi pickup: <b>6</b>" in msg
    assert "TMLG" in msg
    assert "20016512" in msg


def test_format_out_of_stock_pakai_data_prev():
    prev = {"status": "available", "final_price": 23000, "name": "Sunco"}
    msg = format_message("out_of_stock", None, prev, CFG, ITEM)
    assert "STOK HABIS" in msg
    assert "Sunco" in msg  # nama dari prev, bukan crash karena current None
