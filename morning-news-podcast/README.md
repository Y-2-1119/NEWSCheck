# 毎朝ニュースブリーフィング（自分専用ポッドキャスト）

RSSを集めて Gemini無料枠で要約し、日本語音声（edge-tts）にして、
自分専用のポッドキャストとして配信する仕組み。GitHub Actions が毎朝自動実行する。
**ランニングコストは原則 ¥0。**

```
RSS収集 → Gemini要約 → edge-ttsで音声化 → ポッドキャストRSS生成 → GitHub Pagesで配信
                                                              ↓
                                          Android の AntennaPod が夜間に自動DL → 朝に再生
```

---

## セットアップ（一度だけ）

### 1. リポジトリを用意
- GitHubで**新規リポジトリ**を作り、この一式を入れる（`docs/audio/.gitkeep` も残す）。
- 無料アカウントで Pages を使うには **public リポジトリ**にする必要がある（後述のプライバシー注記参照）。

### 2. Gemini APIキーを取得（無料）
- Google AI Studio でAPIキーを発行（クレカ登録不要）。
- リポジトリの **Settings → Secrets and variables → Actions → Secrets** に
  `GEMINI_API_KEY` という名前で登録。

### 3. 配信URL（BASE_URL）を設定
- 同じ画面の **Variables** タブに `BASE_URL` を登録。値は
  `https://<ユーザー名>.github.io/<リポジトリ名>` （末尾スラッシュなし）。

### 4. GitHub Pages を有効化
- **Settings → Pages** で、Source = `Deploy from a branch`、Branch = `main` / フォルダ `/docs` を選択。

### 5. 初回実行
- **Actions** タブ → "Daily News Podcast" → **Run workflow** で手動実行。
- 成功すると `docs/feed.xml` と `docs/audio/YYYY-MM-DD.mp3` が生成・push される。
- フィードURLは `https://<ユーザー名>.github.io/<リポジトリ名>/feed.xml`。

### 6. Android（AntennaPod）で登録
1. Google Play で **AntennaPod** をインストール。
2. 「+」→ **RSSアドレスを追加** に上記フィードURLを貼る。
3. 設定 → **自動ダウンロード** をON。時間帯を**夜間・Wi-Fi時のみ**に。
4. 朝はウィジェット/Bluetoothで再生するだけ。再生速度も調整可。

以降は毎朝 04:30 JST に自動で新エピソードが作られる（時刻は `.github/workflows/daily.yml` のcronで調整）。

---

## チューニング
- ソースの追加・削除・トピック変更 → `feeds.yaml` を編集。
- 1本あたりの長さ → `max_items_per_category`。
- 声 → `voice`（`ja-JP-NanamiNeural` 女性 / `ja-JP-KeitaNeural` 男性）。

---

## プライバシーを上げたい場合（任意）
GitHub Pages は **Basic認証に非対応**で、無料だとリポジトリも public のため、
**フィードURLを知られると誰でも聴ける**（中身は公開ニュースの要約なので実害は小さい）。
本当に鍵をかけたいなら `cloudflare-worker/worker.js` を使い、AntennaPod の
「パスワード保護フィード」として登録する（Cloudflare無料枠で¥0）。手順はファイル内コメント参照。

---

## コスト
- Gemini：無料枠（gemini-2.5-flash, 1日250リクエスト程度）。1日数十本の要約なら余裕。
- GitHub Actions：毎日5分程度の実行で無料枠内。
- 合計 **¥0**。

## 正直なリスク・注意点
- **要約は誤りうる**：本文が薄い記事は「足さない」方針で見出しのみ読む設計。鵜呑みにせず、ショーノートの原文リンクで確認する前提。
- **edge-tts は非公式**：将来壊れたら Google Cloud TTS無料枠 / gTTS 等へ差し替えが必要。
- **Googleニュース検索RSSは関連度にムラ**：無関係な記事が混じることがある。`feeds.yaml` のクエリ調整が定期的に要る。
- **モデル名の廃止**：Geminiは更新が速い。動かなくなったら `GEMINI_MODEL` を新モデルに変更。
- **cronの時刻は数分ずれる**：起床より早めに設定済み。
- **保守の主体は自分**：RSS変更・モデル差し替え対応は年数回発生する。
- **著作権**：見出し＋短い自作要約＋リンクに留める設計（全文転載はしない）。
