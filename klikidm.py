"""
Klien API KlikIndomaret (hasil reverse engineering).

Gateway utama:
    https://ap-mc.klikindomaret.com/assets-klikidm{svc}/api/get/catalog-xpress/api/webapp/{endpoint}

Service yang dikenal:
    klikidmcore      - pencarian & daftar produk (search/result)
    klikidmgroceries - detail produk (product/detail-page), category/meta
    klikidmsearch    - search/suggestion
    klikidmorder     - stores/search, search/configuration

Catatan anti-bot:
    Frontend www.klikindomaret.com dilindungi AWS WAF + Cloudflare.
    API gateway ap-mc bisa diakses langsung TANPA cookie selama
    request tidak terlalu agresif -> jaga jeda antar request,
    pakai User-Agent browser, dan backoff saat mendapat 403/429.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

log = logging.getLogger("klikidm")

BASE = "https://ap-mc.klikindomaret.com"
SEARCH_RESULT = "/assets-klikidmcore/api/get/catalog-xpress/api/webapp/search/result"
DETAIL_PAGE = "/assets-klikidmgroceries/api/get/catalog-xpress/api/webapp/product/detail-page"
STORES_SEARCH = "/assets-klikidmorder/api/get/catalog-xpress/api/webapp/stores/search"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

PRODUCT_URL = "https://www.klikindomaret.com/xpress/{permalink}"


@dataclass
class Product:
    """Field produk yang relevan untuk monitoring stok."""

    plu: str
    permalink: str
    name: str
    price: Optional[int] = None
    final_price: Optional[int] = None
    discount_text: Optional[str] = None
    selling: Optional[bool] = None
    image_url: Optional[str] = None
    uom: Optional[str] = None
    url: str = ""
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: Dict[str, Any]) -> "Product":
        permalink = d.get("permalink") or ""
        return cls(
            plu=str(d.get("plu") or ""),
            permalink=permalink,
            name=d.get("productName") or "",
            price=d.get("price"),
            final_price=d.get("finalPrice") if d.get("finalPrice") is not None else d.get("price"),
            discount_text=d.get("discountText"),
            selling=bool(d.get("selling")) if d.get("selling") is not None else None,
            image_url=d.get("imageUrl") or d.get("thumbnail"),
            uom=d.get("uom"),
            url=PRODUCT_URL.format(permalink=permalink) if permalink else "",
            raw=d,
        )


class ApiError(Exception):
    pass


class KlikIndomaretClient:
    def __init__(
        self,
        store_code: str = "TJKT",
        district_id: str = "141100100",
        latitude: float = -6.1763897,
        longitude: float = 106.82667,
        mode: str = "DELIVERY",
        request_delay: float = 3.0,
        timeout: float = 25.0,
    ):
        self.store_code = store_code
        self.district_id = district_id
        self.latitude = latitude
        self.longitude = longitude
        self.mode = mode
        self.request_delay = request_delay
        self.timeout = timeout
        self._last_request_at = 0.0
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "id-ID,id;q=0.9",
                "Origin": "https://www.klikindomaret.com",
                "Referer": "https://www.klikindomaret.com/",
                "Connection": "keep-alive",
            }
        )

    # ------------------------------------------------------------------ util

    def _store_qs(self) -> str:
        return (
            f"storeCode={self.store_code}&latitude={self.latitude}"
            f"&longitude={self.longitude}&mode={self.mode}&districtId={self.district_id}"
        )

    def _get_json(self, path: str, params: Dict[str, str]) -> Dict[str, Any]:
        # jaga jeda minimum antar request agar tidak memicu Cloudflare
        wait = self.request_delay - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        try:
            resp = self.session.get(BASE + path, params=params, timeout=self.timeout)
        except requests.RequestException as e:
            raise ApiError(f"Kesalahan jaringan: {e}") from e
        finally:
            self._last_request_at = time.monotonic()

        if resp.status_code == 403 or resp.status_code == 429:
            # Cloudflare/AWS WAF sedang challenge -> caller harus backoff
            raise ApiError(f"HTTP {resp.status_code} (anti-bot). Turunkan frekuensi polling.")
        if resp.status_code != 200:
            raise ApiError(f"HTTP {resp.status_code} untuk {path}")

        try:
            body = resp.json()
        except ValueError as e:
            raise ApiError(f"Respons bukan JSON dari {path}") from e

        if body.get("status") not in ("00", 0, "0", None):
            # API memakai status "00" = sukses
            log.warning("API status %s: %s", body.get("status"), body.get("message"))
        return body.get("data") or {}

    # -------------------------------------------------------------- endpoint

    def search(self, keyword: str = "", page: int = 0, size: int = 18) -> List[Product]:
        """Cari produk. keyword boleh berupa nama produk atau PLU."""
        data = self._get_json(
            SEARCH_RESULT,
            {
                "page": str(page),
                "size": str(size),
                "categories": "",
                "keyword": keyword,
                **{k: str(v) for k, v in self._store_qs_parts().items()},
            },
        )
        content = data.get("content") or []
        return [Product.from_api(p) for p in content]

    def _store_qs_parts(self) -> Dict[str, Any]:
        return {
            "storeCode": self.store_code,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "mode": self.mode,
            "districtId": self.district_id,
        }

    def find_by_plu(self, plu: str) -> Optional[Product]:
        """Cari tepat satu produk berdasarkan PLU (kode toko digital Indomaret)."""
        products = self.search(keyword=str(plu), size=10)
        for p in products:
            if p.plu == str(plu):
                return p
        return None

    def get_detail(self, permalink: str) -> Optional[Dict[str, Any]]:
        """
        Detail produk (endpoint yang dipakai halaman PDP situs).
        Endpoint ini lebih ketat dari sisi anti-bot; bisa saja 403.
        Karena itu fungsi ini bersifat opsional dan aman gagal.
        """
        try:
            return self._get_json(
                DETAIL_PAGE,
                {**{k: str(v) for k, v in self._store_qs_parts().items()}, "permalink": permalink},
            )
        except ApiError as e:
            log.info("detail-page gagal (%s) — fallback ke search/result", e)
            return None

    def nearby_stores(self) -> List[Dict[str, Any]]:
        """Toko yang melayani district ini (mode DELIVERY = 1 toko gudang)."""
        data = self._get_json(STORES_SEARCH, {"districtId": self.district_id})
        if isinstance(data, dict):
            return [data]
        return data if isinstance(data, list) else []


def fetch_product(client: KlikIndomaretClient, item: Dict[str, Any]) -> Optional[Product]:
    """
    Ambil kondisi terkini satu produk dari item watchlist {"plu", "permalink"}.
    Prioritas: PLU via search/result (paling andal); permalink via detail-page,
    fallback search. Return None bila produk tidak ada di katalog.
    """
    plu = str(item.get("plu") or "").strip()
    permalink = str(item.get("permalink") or "").strip()

    if plu:
        return client.find_by_plu(plu)

    if permalink:
        detail = client.get_detail(permalink)
        if detail:
            prod = detail.get("product") or detail.get("detail")
            if isinstance(prod, dict) and prod.get("permalink"):
                return Product.from_api(prod)
        # fallback: pencarian, lalu cocokkan permalink persis
        found = client.search(keyword=permalink.replace("-", " "), size=10)
        return next((p for p in found if p.permalink == permalink), None)

    return None
