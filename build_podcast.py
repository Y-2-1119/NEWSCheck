#!/usr/bin/env python3
"""毎朝のニュース音声ブリーフィングを作るパイプライン（整理版）。

  RSS収集 -> ノイズ除去/整形 -> Gemini無料枠で1文要約 -> edge-tts音声化 -> ポッドキャストRSS生成

GitHub Actions から1日1回実行する想定。生成物は docs/ に置かれ、GitHub Pages（/docs）で配信。
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
from urllib.parse import urlparse

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
STATE_FILE = DOCS / "state.json"
EPISODES_FILE = DOCS / "episodes.json"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()
BASE_URL = os.environ.get("BASE_URL", "").strip().rstrip("/")

JP_RE = re.compile(r"[\u3040-\u30ff\u4e00-\u9faf\uff66-\uff9f]")


# ----------------------------- ユーティリティ -----------------------------

def clean_text(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def jp_ratio(s: str) -> float:
    chars = [c for c in s if not c.isspace()]
    if not chars:
        return 0.0
    return sum(1 for c in chars if JP_RE.match(c)) / len(chars)


def entry_datetime(entry):
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


def source_info(entry, feed):
    """(媒体名, ドメイン) を返す。"""
    src = entry.get("source")
    name, href = "", ""
    if isinstance(src, dict):
        name = src.get("title", "") or ""
        href = src.get("href", "") or ""
    if not name and hasattr(feed, "feed"):
        name = feed.feed.get("title", "") or ""
    domain = urlparse(href or entry.get("link", "")).netloc.lower().replace("www.", "")
    return name.strip(), domain


def tidy_title(title: str, source_name: str) -> str:
    """Googleニュースの『見出し - 媒体名』の末尾媒体名を落とす。"""
    title = clean_text(title)
    if source_name and title.endswith(source_name):
        title = title[: -len(source_name)].rstrip(" -–—|｜").strip()
    # それ以外の末尾 " - 媒体" も控えめに除去
    title = re.sub(r"\s[-–—|｜]\s[^-–—|｜]{1,18}$", "", title).strip()
    return title


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
    s = cfg.get("settings", {})
    lookback = int(s.get("lookback_hours", 24))
    max_per = int(s.get("max_items_per_category", 3))
    min_jp = float(s.get("min_japanese_ratio", 0.4))
    block = {d.lower().replace("www.", "") for d in s.get("block_domains", [])}
    cutoff = datetime.datetime.now(tz=JST) - datetime.timedelta(hours=lookback + 2)
    now_iso = datetime.datetime.now(tz=JST).isoformat()

    seen = load_seen()
    seen_titles_global = set()  # カテゴリをまたいだ重複も防ぐ
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
                name, domain = source_info(e, feed)
                title = tidy_title(e.get("title", ""), name)
                link = e.get("link", "")
                if not title:
                    continue
                if domain in block:                 # 海外/不要ドメインを除外
                    continue
                if jp_ratio(title) < min_jp:         # 日本語が薄い見出しを除外
                    continue
                dt = entry_datetime(e)
                if dt and dt < cutoff:
                    continue
                key = link or title
                if key in seen or title in seen_titles_global:
                    continue
                bucket.append({
                    "title": title, "link": link,
                    "desc": clean_text(e.get("summary", "")),
                    "source": name, "dt": dt,
                })

        # カテゴリ内のタイトル重複除去
        uniq, titles = [], set()
        for it in bucket:
            if it["title"] in titles:
                continue
            titles.add(it["title"])
            uniq.append(it)

        uniq.sort(key=lambda x: x["dt"] or cutoff, reverse=True)
        uniq = uniq[:max_per]

        for it in uniq:
            seen[it["link"] or it["title"]] = now_iso
            seen_titles_global.add(it["title"])

        if uniq:
            sections.append({"name": cat["name"], "items": uniq})

    save_json(STATE_FILE, seen)
    return sections


# ----------------------------- 要約（Gemini無料枠） -----------------------------

def gemini_summarize(title: str, desc: str):
    """記事テキストだけを根拠に1文要約。薄い/失敗時は None（=見出しのみ読む）。"""
    text = desc if len(desc) >= 40 else ""
    if not text or not GEMINI_API_KEY:
        return None

    prompt = (
        "次のニュース本文だけを根拠に、日本語で『1文だけ』要点を述べてください。\n"
        "条件: 40〜60字程度 / 句点は1つ / 「要約」「概要」等のラベルや記号・箇条書きは付けない / "
        "本文に無い情報を足さない・推測しない・数字や固有名詞を捏造しない / 文だけを返す。\n\n"
        f"見出し: {title}\n本文: {text}"
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 128},
    }
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}")

    for attempt in range(4):
        try:
            r = requests.post(url, json=body, timeout=60)
            if r.status_code == 429:
                time.sleep(8 * (attempt + 1))
                continue
            r.raise_for_status()
            cands = r.json().get("candidates", [])
            if not cands:
                return None
            parts = cands[0].get("content", {}).get("parts", [])
            out = "".join(p.get("text", "") for p in parts).strip()
            # 念のため先頭ラベルを除去（「要約：」など）
            out = re.sub(r"^[^：:]{0,6}[：:]\s*", "", out).strip()
            out = out.replace("\n", " ").strip()
            return out or None
        except Exception as e:
            print(f"[warn] Gemini要約失敗: {e}")
            time.sleep(4)
    return None


# ----------------------------- 台本＆ショーノート -----------------------------

def build_script_and_notes(sections, today):
    wd = "月火水木金土日"[today.weekday()]
    date_jp = f"{today.month}月{today.day}日"
    total = sum(len(s["items"]) for s in sections)

    lines = [
        f"おはようございます。{date_jp}、{wd}曜日のニュースブリーフィングです。",
        f"本日は{len(sections)}つのカテゴリー、あわせて{total}件をお届けします。",
    ]
    notes = [f"{date_jp} ニュースブリーフィング", ""]

    for i, sec in enumerate(sections, 1):
        n = len(sec["items"])
        lines.append("")  # 段落の間（音声の区切り）
        lines.append(f"カテゴリー{i}、{sec['name']}。{n}件です。")
        notes.append(f"■ {sec['name']}")
        for j, it in enumerate(sec["items"], 1):
            summary = it.get("summary")
            if summary:
                lines.append(f"{j}件目。{it['title']}。{summary}")
            else:
                lines.append(f"{j}件目。{it['title']}。")
            note = f"{j}. {it['title']}"
            if it.get("source"):
                note += f"（{it['source']}）"
            notes.append(note)
            if it.get("link"):
                notes.append(f"   {it['link']}")
        notes.append("")

    lines.append("")
    lines.append("以上で今朝のブリーフィングを終わります。詳しくは説明欄のリンクをご確認ください。よい一日を。")
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
            time.sleep(1)

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
