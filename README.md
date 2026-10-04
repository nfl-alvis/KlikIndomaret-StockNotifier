# StockNotifier KlikIndomaret 🛒➡️📣

Notifikasi **Telegram** saat produk di [klikindomaret.com](https://www.klikindomaret.com) masuk kembali stok ("barang masuk"), habis, atau harganya turun.

Hasil reverse engineering API KlikIndomaret (web + APK Android) dan implementasi poller-nya — tanpa perlu login akun, cukup endpoint publik.

---

## Hasil Reverse Engineering

### Arsitektur

Frontend `www.klikindomaret.com` = beberapa sub-aplikasi Next.js (`assets-klikidmcore`, `assets-klikidmgroceries`, `assets-klikidmsearch`, `assets-klikidmorder`, `assets-klikidmauth`, `assets-klikidmprofile`). Semua data produk diambil dari **API gateway**:

```
https://ap-mc.klikindomaret.com/assets-klikidm{svc}/api/get/catalog-xpress/api/webapp/{endpoint}
```

- Frontend dilindungi AWS WAF + Cloudflare (JS challenge).
- **API gateway-nya bisa diakses langsung** (tanpa cookie/WAF token) selama frekuensi request wajar — pola URL dibaca dari trafik browser, bukan dari APK (string API di APK ter-obfuscate R8).

### Endpoint yang ditemukan

| Fungsi | Service | Endpoint |
|---|---|---|
| Cari/daftar produk (mendukung `keyword` = nama maupun **PLU**) | `klikidmcore` | `search/result` |
| Detail produk (halaman PDP) | `klikidmgroceries` | `product/detail-page` |
| Produk terkait | `klikidmgroceries` | `product/related-product` |
| Saran pencarian | `klikidmsearch` | `search/suggestion` |
| Konfigurasi pencarian | `klikidmorder` | `search/configuration` |
| Daftar toko per area | `klikidmorder` | `stores/search` |
| Kategori | `klikidmgroceries` | `category/meta` |
| Section beranda | `klikidmcore` | `home/webapp/xpress/api/mobile/section` |

### Contoh: cek produk by PLU

```bash
curl "https://ap-mc.klikindomaret.com/assets-klikidmcore/api/get/catalog-xpress/api/webapp/search/result?page=0&size=5&categories=&keyword=20143793&storeCode=TJKT&latitude=-6.1763897&longitude=106.82667&mode=DELIVERY&districtId=141100100"
```

Respon (ringkas):

```json
{
  "status": "00",                       // "00" = sukses
  "data": {
    "totalElements": 1,
    "content": [{
      "plu": "20143793",
      "permalink": "indomaret-indomaret-food-container-pcs-640-ml",
      "productName": "Indomaret Food Container 640mL",
      "price": 38700,
      "finalPrice": 19350,
      "discountText": "50%",
      "selling": true,                  // ← flag ketersediaan utama
      "imageUrl": "https://cdn-klik.klikindomaret.com/klik-catalog/product/20143793_1.jpg",
      "uom": "PCS (1 pcs)"
    }]
  }
}
```

Parameter wajib: `storeCode` (kode toko), `districtId` (kelurahan/kecamatan), `latitude`/`longitude`, `mode` (`DELIVERY`/`PICKUP`).

### URL halaman produk

```
https://www.klikindomaret.com/xpress/{permalink}
https://www.klikindomaret.com/xpress/{PLU}      # redirect 307 ke permalink
```

### Catatan anti-bot

Request beruntun tanpa jeda memicu Cloudflare 403. Poller ini menjaga jeda minimum antar request (`request_delay_seconds`), interval polling wajar (`interval_seconds` + jitter acak), dan backoff otomatis saat mulai gagal.

---

## Cara Pakai

### 1. Instalasi

```bash
pip install -r requirements.txt
```

### 2. Buat bot Telegram

1. Chat [@BotFather](https://t.me/BotFather) → `/newbot` → ikuti langkahnya → salin **token** (`123456:ABC-...`).
2. Cari **chat id**: kirim pesan apa saja ke bot Anda, lalu buka
   `https://api.telegram.org/bot<TOKEN>/getUpdates` → lihat `"chat":{"id": 123456789}`.
   (Alternatif: chat [@userinfobot](https://t.me/userinfobot).)
3. **Penting**: kirim minimal 1 pesan `/start` ke bot Anda, kalau tidak bot tidak bisa mengirim ke Anda.

### 3. Konfigurasi

```bash
copy config.example.json config.json   # Windows (atau: cp)
```

Isi `telegram.bot_token` dan `telegram.chat_id` di `config.json` — **atau** biarkan kosong dan set env var `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` (cara ini yang dipakai GitHub Actions). Atur `store` sesuai lokasi Anda (lihat `python main.py stores`), dan `notify_on` untuk memilih jenis notifikasi.

### 4. Isi watchlist

```bash
python main.py add 20143793                                  # by PLU
python main.py add "energen cokelat" --index 0               # by kata kunci
python main.py add https://www.klikindomaret.com/xpress/...  # by URL
python main.py list
python main.py remove 20143793
```

### 5. Jalankan

```bash
python main.py test-telegram   # sekali, untuk memastikan bot jalan
python main.py check           # cek satu siklus, tampilkan hasil
python main.py watch           # mode utama: pantau terus-menerus
```

Contoh notifikasi:

```
🟢 BARANG MASUK!

📦 Indomaret Food Container 640mL
💵 Harga: Rp 19.350
🏷️ Diskon: 50%

🏪 Toko: TJKT (DELIVERY)
🔗 https://www.klikindomaret.com/xpress/indomaret-indomaret-food-container-pcs-640-ml
🔢 PLU: 20143793
```

### Deteksi "barang masuk"

Per produk disimpan status terakhir di `state.json`. Notifikasi terkirim saat transisi:

| Sebelum | Sesudah | Notifikasi |
|---|---|---|
| tidak tersedia (`selling:false`/hilang) | `selling:true` | 🟢 **barang masuk** |
| tersedia | habis/hilang | 🔴 stok habis (opsional) |
| harga lebih tinggi | harga turun | 💰 harga turun |
| tanpa diskon | ada diskon | 🏷️ diskon baru |

> Siklus pertama hanya menyimpan baseline — notifikasi mulai dari perubahan kedua dst.

---

## Menjalankan 24/7 via GitHub Actions (tanpa PC nyala)

Workflow [`.github/workflows/stock-notifier.yml`](.github/workflows/stock-notifier.yml) menjalankan `python main.py check` tiap 30 menit di server GitHub, mengirim notifikasi Telegram, lalu menyimpan `state.json` kembali ke repo — laptop bisa mati total.

1. Buat repo GitHub (boleh **private**) dan push folder ini:

   ```bash
   git init -b main
   git add -A
   git commit -m "StockNotifier KlikIndomaret"
   git remote add origin https://github.com/<username>/<repo>.git
   git push -u origin main
   ```

2. Tambahkan **2 secrets** — Settings → Secrets and variables → Actions → *New repository secret*:
   - `TELEGRAM_BOT_TOKEN` → token dari BotFather
   - `TELEGRAM_CHAT_ID` → chat id Anda

   Atau lewat CLI: `gh secret set TELEGRAM_BOT_TOKEN` lalu `gh secret set TELEGRAM_CHAT_ID`.

3. Tab **Actions** → pilih **Stock Notifier** → **Run workflow** untuk uji manual. Setelah itu cron berjalan sendiri tiap 30 menit.

Catatan:

- Token **tidak pernah ikut dalam kode** — `config.json` di repo sengaja tanpa rahasia; secrets disuntik sebagai env var saat run.
- `state.json` di-commit ulang otomatis tiap run (pesan `[skip ci]`), jadi deteksi transisi stok tersambung antar-run. Perubahan state juga dihitung sebagai aktivitas repo, sehingga cron tidak dinonaktifkan aturan 60-hari GitHub.
- Interval default `*/5` menit = minimum GitHub Actions. Agar tetap **gratis**, repo harus **public** (kuota menit tak terbatas). Repo private hanya dapat 2.000 menit/bulan, sedangkan `*/5` butuh ±8.800 menit/bulan (sisanya ±$0.008/menit) — kalau tetap private, pakai `*/30`.
- Jadwal cron GitHub bersifat *best-effort*: pada jam sibuk run bisa tertunda antrean, jadi jeda efektif kadang melebar jadi 5–15 menit. Kalau butuh 5 menit yang konsisten, jalankan `python main.py watch` di server/VPS kecil sebagai gantinya.
- Pola polling tetap ramah: tiap run hanya 1 request per produk dengan jeda, dan backoff otomatis kalau Cloudflare mulai menolak.
- Tambah produk saat berjalan di Actions: edit `config.json` (tambah produk), push — siklus berikutnya langsung memantau.

---

## Struktur Proyek

```
main.py             CLI (watch/check/add/list/remove/test-telegram/stores)
klikidm.py          Klien API KlikIndomaret (search/result, detail-page, stores)
monitor.py          Loop polling, deteksi transisi stok/harga, format pesan
telegram_notify.py  Pengirim pesan Telegram Bot API
.github/workflows/  Workflow GitHub Actions (cek berkala + commit state)
config.json         Watchlist + pengaturan (ikut repo, TANPA rahasia di dalamnya)
state.json          Status terakhir per produk (ikut repo agar CI tersambung)
```

## Catatan

- Gunakan secara wajar untuk kebutuhan pribadi; interval polling default 5 menit ± jitter, 1 request per produk per siklus. Jangan dikecilkan terlalu agresif — Cloudflare akan menolak dan IP bisa dikunci sementara.
- Struktur API dapat berubah sewaktu-waktu oleh Indomaret; jika tiba-tiba semua gagal, cek kembali pola endpoint di atas lewat DevTools browser (tab Network, filter `ap-mc`).
