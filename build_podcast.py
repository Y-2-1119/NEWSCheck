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
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime.datetime(*t[:6], tzinfo=datetime.timezone.utc).astimezone(JST)
    for key in ("published", "updated"):
        v = entry.get(key)
        if v:
            try:
                return dateparser.parse(v).astimezone(JST)
            except Exception:
                pass
    return None


def source_name(entry, feed) -> str:
    src = entry.get("source")
    if isinstance(src, dict) and src.get("title"):
        return src["title"]
    title = feed.feed.get("title", "") if hasattr(feed, "feed") else ""
    return title or ""


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ----------------------------- 収集 -----------------------------

def load_seen():
    """{key: ISO日時} を読み、7日より古いものは捨てる。"""
    seen = load_json(STATE_FILE, {})
    cutoff = datetime.datetime.now(tz=JST) - datetime.timedelta(days=7)
    pruned = {}
    for k, v in seen.items():
        try:
            if dateparser.parse(v) >= cutoff:
                pruned[k] = v
        except Exception:
            continue
    return pruned


def collect_items(cfg):
    settings = cfg.get("settings", {})
    lookback = int(settings.get("lookback_hours", 24))
    max_per = int(settings.get("max_items_per_category", 4))
    cutoff = datetime.datetime.now(tz=JST) - datetime.timedelta(hours=lookback + 2)
    now_iso = datetime.datetime.now(tz=JST).isoformat()

    seen = load_seen()
    sections = []

    for cat in cfg.get("categories", []):
        bucket = []
        for url in cat.get("feeds", []):
            try:
                feed = feedparser.parse(url)
            except Exception as e:
                print(f"[warn] フィード取得失敗 {url}: {e}")
                continue
            if getattr(feed, "bozo", 0) and not feed.entries:
                print(f"[warn] フィード解析不可（スキップ）: {url}")
                continue
            for e in feed.entries:
                title = clean_text(e.get("title", ""))
                link = e.get("link", "")
                if not title:
                    continue
                dt = entry_datetime(e)
                if dt and dt < cutoff:
                    continue
                key = link or title
                if key in seen:
                    continue
                bucket.append({
                    "title": title,
                    "link": link,
                    "desc": clean_text(e.get("summary", "")),
                    "source": source_name(e, feed),
                    "dt": dt,
                })

        # カテゴリ内でタイトル重複を除去
        uniq, titles = [], set()
        for it in bucket:
            t = it["title"]
            if t in titles:
                continue
            titles.add(t)
            uniq.append(it)

        uniq.sort(key=lambda x: x["dt"] or cutoff, reverse=True)
        uniq = uniq[:max_per]

        for it in uniq:
            seen[it["link"] or it["title"]] = now_iso

        if uniq:
            sections.append({"name": cat["name"], "items": uniq})

    save_json(STATE_FILE, seen)
    return sections


# ----------------------------- 要約（Gemini無料枠） -----------------------------

def gemini_summarize(title: str, desc: str):
    """記事テキストだけを根拠に2文要約。見出ししか無い/失敗時は None（=見出しのみ読む）。"""
    text = desc if len(desc) >= 40 else ""
    if not text or not GEMINI_API_KEY:
        return None

    prompt = (
        "あなたはニュース要約アシスタントです。次の記事テキストだけを根拠に、"
        "日本語で2文以内に要約してください。\n"
        "厳守事項: テキストに無い情報を足さない / 推測しない / 固有名詞・数字を捏造しない / "
        "話し言葉で簡潔に / 「要約:」などのラベルや前置きは付けない。\n\n"
        f"見出し: {title}\n本文: {text}"
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 256},
    }
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}")

    for attempt in range(4):
        try:
            r = requests.post(url, json=body, timeout=60)
            if r.status_code == 429:  # レート制限 -> 待って再試行
                time.sleep(8 * (attempt + 1))
                continue
            r.raise_for_status()
            data = r.json()
            cands = data.get("candidates", [])
            if not cands:  # セーフティ等で空 -> 見出しのみに
                return None
            parts = cands[0].get("content", {}).get("parts", [])
            out = "".join(p.get("text", "") for p in parts).strip()
            return out or None
        except Exception as e:
            print(f"[warn] Gemini要約失敗: {e}")
            time.sleep(4)
    return None


# ----------------------------- 台本＆ショーノート -----------------------------

def build_script_and_notes(sections, today):
    wd = "月火水木金土日"[today.weekday()]
    date_jp = f"{today.month}月{today.day}日"

    lines = [f"おはようございます。{date_jp}、{wd}曜日のニュースブリーフィングです。"]
    notes = [f"{date_jp}のニュースブリーフィング", ""]

    for sec in sections:
        lines.append(f"続いて、{sec['name']}です。")
        notes.append(f"■ {sec['name']}")
        for it in sec["items"]:
            summary = it.get("summary")
            if summary:
                lines.append(f"{it['title']}。{summary}")
            else:
                lines.append(f"{it['title']}。")
            note = f"・{it['title']}"
            if it.get("source"):
                note += f"（{it['source']}）"
            if it.get("link"):
                note += f"\n{it['link']}"
            notes.append(note)
        notes.append("")

    lines.append("以上、今朝のブリーフィングでした。詳しくは説明欄のリンクをご確認ください。よい一日を。")
    return "\n".join(lines), "\n".join(notes)


# ----------------------------- 音声合成 -----------------------------

async def synthesize(text: str, out_path: Path, voice: str):
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(out_path))


# ----------------------------- ポッドキャストRSS -----------------------------

def build_feed(cfg, episodes):
    p = cfg.get("podcast", {})
    fg = FeedGenerator()
    fg.load_extension("podcast")
    fg.title(p.get("title", "毎朝ニュースブリーフィング"))
    fg.link(href=BASE_URL or "https://example.com", rel="alternate")
    fg.description(p.get("description", "自分専用の朝のニュース要約"))
    fg.language("ja")
    fg.podcast.itunes_author(p.get("author", "me"))
    fg.podcast.itunes_category(p.get("category", "News"))
    fg.podcast.itunes_explicit("no")

    # 新しい順に並べて登録
    for ep in sorted(episodes, key=lambda e: e["id"], reverse=True):
        fe = fg.add_entry()
        fe.id(ep["url"])
        fe.title(ep["title"])
        fe.description(ep["notes"])
        fe.enclosure(ep["url"], str(ep["size"]), "audio/mpeg")
        fe.published(dateparser.parse(ep["pubDate"]))

    fg.rss_file(str(DOCS / "feed.xml"))


# ----------------------------- メイン -----------------------------

def main():
    if not BASE_URL:
        print("[warn] BASE_URL が未設定です。enclosure の音声URLが不正になります。")

    cfg = yaml.safe_load((ROOT / "feeds.yaml").read_text(encoding="utf-8"))
    today = datetime.datetime.now(tz=JST)

    sections = collect_items(cfg)
    if not sections:
        print("新着なし。今日はエピソードを作りません。")
        return

    for sec in sections:
        for it in sec["items"]:
            it["summary"] = gemini_summarize(it["title"], it["desc"])
            time.sleep(1)  # 無料枠のレート制限にやさしく

    script, notes = build_script_and_notes(sections, today)

    AUDIO.mkdir(parents=True, exist_ok=True)
    date_id = today.strftime("%Y-%m-%d")
    mp3 = AUDIO / f"{date_id}.mp3"
    asyncio.run(synthesize(script, mp3, cfg["settings"].get("voice", "ja-JP-NanamiNeural")))

    episodes = [e for e in load_json(EPISODES_FILE, []) if e.get("id") != date_id]
    episodes.append({
        "id": date_id,
        "title": f"{today.month}月{today.day}日のニュースブリーフィング",
        "url": f"{BASE_URL}/audio/{date_id}.mp3",
        "size": mp3.stat().st_size,
        "notes": notes,
        "pubDate": today.isoformat(),
    })

    # 保持期間を超えた古い音声を削除
    keep = int(cfg["settings"].get("retention_days", 14))
    episodes.sort(key=lambda e: e["id"], reverse=True)
    episodes = episodes[:keep]
    keep_ids = {e["id"] for e in episodes}
    for f in AUDIO.glob("*.mp3"):
        if f.stem not in keep_ids:
            f.unlink()

    save_json(EPISODES_FILE, episodes)
    build_feed(cfg, episodes)
    print(f"完了: {mp3}（{mp3.stat().st_size/1024:.0f} KB）/ エピソード数 {len(episodes)}")


if __name__ == "__main__":
    main()
