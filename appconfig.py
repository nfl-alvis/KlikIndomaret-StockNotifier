"""Load/save config.json — dipakai main.py dan bot.py (agar tidak duplikasi)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"


def load_config(quiet: bool = False) -> dict:
    if not CONFIG_FILE.exists():
        example = BASE_DIR / "config.example.json"
        if example.exists():
            CONFIG_FILE.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
            if not quiet:
                print(
                    "config.json belum ada — dibuat salinan dari config.example.json.\n"
                    f"Isi telegram.bot_token & telegram.chat_id di {CONFIG_FILE}, lalu jalankan lagi."
                )
                sys.exit(1)
            return {}
        print("config.json tidak ditemukan!")
        sys.exit(1)
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
