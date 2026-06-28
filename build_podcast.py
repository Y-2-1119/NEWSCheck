#!/usr/bin/env python3
"""毎朝のニュース音声ブリーフィングを作るパイプライン。

  RSS収集 -> Gemini無料枠で要約 -> edge-ttsで日本語音声化 -> ポッドキャストRSS生成

GitHub Actions から1日1回実行する想定。生成物は docs/ に置かれ、
GitHub Pages（/docs）でそのまま配信される。
"""

import os
import re
import html
import json
import time
import asyncio
import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
import requests
import feedparser
import edge_tts
from dateutil import parser as dateparser
from feedgen.feed import FeedGenerator

JST = ZoneInfo("Asia/Tokyo")
ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
AUDIO = DOCS / "audio"
STATE_FILE = DOCS / "state.json"        # 既出記事の記録（重複防止）
EPISODES_FILE = DOCS / "episodes.json"  # 配信中エピソードの台帳

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()
# 例: https://USERNAME.github.io/REPO  （末尾スラッシュなし）
BASE_URL = os.environ.get("BASE_URL", "").strip().rstrip("/")


# ----------------------------- ユーティリティ -----------------------------

def clean_text(s: str) -> str:
    """HTMLタグ・実体参照を落として素のテキストにする。"""
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def entry_datetime(entry):
    """フィード項目の日時を JST の aware datetime で返す（取れなければ None）。"""
    for key in
