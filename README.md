# StockNotifier KlikIndomaret 🛒➡️📣

Telegram notifications when products come back in stock ("barang masuk"), go out of stock, or drop in price on [klikindomaret.com](https://www.klikindomaret.com).

This is the result of reverse engineering the KlikIndomaret API (web + Android APK) plus its poller implementation — no account login needed, only public endpoints.

---

## Reverse Engineering Findings

### Architecture

The `www.klikindomaret.com` frontend consists of several Next.js sub-apps (`assets-klikidmcore`, `assets-klikidmgroceries`, `assets-klikidmsearch`, `assets-klikidmorder`, `assets-klikidmauth`, `assets-klikidmprofile`). All product data comes from a single **API gateway**:

```
https://ap-mc.klikindomaret.com/assets-klikidm{svc}/api/get/catalog-xpress/api/webapp/{endpoint}
```

- The frontend is protected by AWS WAF + Cloudflare (JS challenge).
- **The API gateway itself is directly accessible** (no cookies/WAF token needed) as long as the request frequency stays reasonable — the URL patterns were read from browser traffic, not from the APK (API strings inside the APK are obfuscated by R8).

### Discovered endpoints

| Purpose | Service | Endpoint |
|---|---|---|
| Search / list products (`keyword` accepts names **and PLU**) | `klikidmcore` | `search/result` |
| Product detail (PDP page) | `klikidmgroceries` | `product/detail-page` |
| Related products | `klikidmgroceries` | `product/related-product` |
| Search suggestions | `klikidmsearch` | `search/suggestion` |
| Search configuration | `klikidmorder` | `search/configuration` |
| Store list per area | `klikidmorder` | `stores/search` |
| Categories | `klikidmgroceries` | `category/meta` |
| Home sections | `klikidmcore` | `home/webapp/xpress/api/mobile/section` |

### Example: check a product by PLU

```bash
curl "https://ap-mc.klikindomaret.com/assets-klikidmcore/api/get/catalog-xpress/api/webapp/search/result?page=0&size=5&categories=&keyword=20143793&storeCode=TJKT&latitude=-6.1763897&longitude=106.82667&mode=DELIVERY&districtId=141100100"
```

Response (abridged):

```json
{
  "status": "00",                       // "00" = success
  "data": {
    "totalElements": 1,
    "content": [{
      "plu": "20143793",
      "permalink": "indomaret-indomaret-food-container-pcs-640-ml",
      "productName": "Indomaret Food Container 640mL",
      "price": 38700,
      "finalPrice": 19350,
      "discountText": "50%",
      "selling": true,                  // ← main availability flag
      "imageUrl": "https://cdn-klik.klikindomaret.com/klik-catalog/product/20143793_1.jpg",
      "uom": "PCS (1 pcs)"
    }]
  }
}
```

Required parameters: `storeCode` (store code), `districtId` (district), `latitude`/`longitude`, `mode` (`DELIVERY`/`PICKUP`).

### Product page URL

```
https://www.klikindomaret.com/xpress/{permalink}
https://www.klikindomaret.com/xpress/{PLU}      # 307-redirects to the permalink URL
```

### Anti-bot notes

Burst requests without pauses trigger a Cloudflare 403. This poller enforces a minimum pause between requests (`request_delay_seconds`), a reasonable polling interval (`interval_seconds` + random jitter), and automatic backoff when failures start.

---

## Usage

### 1. Install

```bash
pip install -r requirements.txt
```

### 2. Create the Telegram bot

1. Chat [@BotFather](https://t.me/BotFather) → `/newbot` → follow the steps → copy the **token** (`123456:ABC-...`).
2. Find your **chat id**: send any message to your bot, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` → look for `"chat":{"id": 123456789}`.
   (Alternative: chat [@userinfobot](https://t.me/userinfobot).)
3. **Important**: send `/start` to your bot at least once, otherwise it cannot message you.

### 3. Configure

```bash
copy config.example.json config.json   # Windows (or: cp)
```

Fill in `telegram.bot_token` and `telegram.chat_id` in `config.json` — **or** leave them empty and set the `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` environment variables instead (this is what the GitHub Actions workflow uses). Adjust `store` to your location (see `python main.py stores`), and `notify_on` to choose which notifications you want.

### 4. Build your watchlist

```bash
python main.py add 20143793                                  # by PLU
python main.py add "energen cokelat" --index 0               # by keyword
python main.py add https://www.klikindomaret.com/xpress/...  # by URL
python main.py list
python main.py remove 20143793
```

### 5. Run

```bash
python main.py test-telegram   # once, to verify the bot works
python main.py check           # single check cycle, prints results
python main.py watch           # main mode: monitor continuously
```

Example notification (actual bot output is in Indonesian):

```
🟢 BARANG MASUK!

📦 Indomaret Food Container 640mL
💵 Harga: Rp 19.350
🏷️ Diskon: 50%

🏪 Toko: TJKT (DELIVERY)
🔗 https://www.klikindomaret.com/xpress/indomaret-indomaret-food-container-pcs-640-ml
🔢 PLU: 20143793
```

### How "back in stock" detection works

The last known status of each product is stored in `state.json`. Notifications are sent on transitions:

| Before | After | Notification |
|---|---|---|
| unavailable (`selling:false`/missing) | `selling:true` | 🟢 **back in stock** |
| available | out of stock/missing | 🔴 out of stock (optional) |
| higher price | price drop | 💰 price drop |
| no discount | discount appears | 🏷️ new discount |

> The first cycle only records a baseline — notifications start from the second change onward.

---

## Running 24/7 with GitHub Actions (no PC needed)

The [`.github/workflows/stock-notifier.yml`](.github/workflows/stock-notifier.yml) workflow runs `python main.py check` every 5 minutes on GitHub's servers, sends the Telegram notification, then commits `state.json` back to the repo — your laptop can be completely off.

1. Create a GitHub repo (**public recommended**, see quota notes) and push this folder:

   ```bash
   git init -b main
   git add -A
   git commit -m "StockNotifier KlikIndomaret"
   git remote add origin https://github.com/<username>/<repo>.git
   git push -u origin main
   ```

2. Add **2 secrets** — Settings → Secrets and variables → Actions → *New repository secret*:
   - `TELEGRAM_BOT_TOKEN` → token from BotFather
   - `TELEGRAM_CHAT_ID` → your chat id

   Or via CLI: `gh secret set TELEGRAM_BOT_TOKEN` then `gh secret set TELEGRAM_CHAT_ID`.

3. **Actions** tab → pick **Stock Notifier** → **Run workflow** to test manually. After that, the cron runs on its own every 5 minutes.

Notes:

- The token **never lives in the code** — `config.json` in the repo intentionally contains no secrets; secrets are injected as env vars during runs.
- `state.json` is re-committed automatically **only when a product's status, price, or discount actually changes** (`[skip ci]` commit message), so stock-transition detection stays connected across runs without flooding the commit history. Those state commits also count as repo activity, so GitHub's 60-day cron deactivation rule never kicks in.
- The default interval `*/5` minutes is GitHub Actions' minimum. To stay **free**, the repo must be **public** (unlimited minutes). Private repos only get 2,000 free minutes/month while `*/5` needs ~8,800 (excess billed at ~$0.008/min) — if you keep it private, switch to `*/30`.
- GitHub's cron is *best-effort*: during peak hours runs can be queued and delayed, so the effective gap sometimes stretches to 5–15 minutes. If you need truly consistent 5-minute checks, run `python main.py watch` on a small server/VPS instead.
- The polling pattern stays gentle: each run makes only 1 request per product with pauses between requests, plus automatic backoff if Cloudflare starts pushing back.
- Adding products while running on Actions: edit `config.json` (add the product), push — the next cycle picks it up.

---

## Project Structure

```
main.py             CLI (watch/check/add/list/remove/test-telegram/stores)
klikidm.py          KlikIndomaret API client (search/result, detail-page, stores)
monitor.py          Polling loop, stock/price transition detection, message formatting
telegram_notify.py  Telegram Bot API message sender
.github/workflows/  GitHub Actions workflow (scheduled checks + state commit)
config.json         Watchlist + settings (committed to the repo, contains NO secrets)
state.json          Last known status per product (committed so CI stays in sync)
```

## Notes

- Use it fairly and for personal needs; the default polling interval is 5 minutes (GitHub Actions) with 1 request per product per cycle. Don't crank it down aggressively — Cloudflare will reject and may temporarily lock your IP.
- The API structure can change at any time without notice; if everything suddenly fails, re-check the endpoint patterns above via browser DevTools (Network tab, filter `ap-mc`).
