// （任意）Basic認証でフィードを保護したい場合のCloudflare Worker。
// 無料枠で動く。AntennaPod は「パスワード保護されたフィード」に対応しているので、
// フィードURLをこのWorkerのドメインにし、ユーザー名/パスワードを設定すれば他人は聴けない。
//
// 設定:
//   1. Cloudflareアカウント -> Workers -> 新規作成し、このコードを貼る
//   2. 環境変数(Settings > Variables)に
//        ORIGIN = https://USERNAME.github.io/REPO   （GitHub Pagesの公開元）
//        USER   = 好きなユーザー名
//        PASS   = 好きなパスワード   （Secret推奨）
//   3. feeds.yaml は使わず、GitHub の変数 BASE_URL を「このWorkerのURL」に変更する。
//      （音声URLもWorker経由になり、認証が効く）
//   4. AntennaPodでこのWorkerの /feed.xml を登録し、ユーザー名/パスワードを入力。

export default {
  async fetch(request, env) {
    const expected = "Basic " + btoa(`${env.USER}:${env.PASS}`);
    const got = request.headers.get("Authorization") || "";
    if (got !== expected) {
      return new Response("Authentication required", {
        status: 401,
        headers: { "WWW-Authenticate": 'Basic realm="podcast"' },
      });
    }
    const url = new URL(request.url);
    const target = env.ORIGIN.replace(/\/$/, "") + url.pathname;
    const resp = await fetch(target, { cf: { cacheTtl: 300 } });
    // ヘッダを引き継いで返す
    const headers = new Headers(resp.headers);
    return new Response(resp.body, { status: resp.status, headers });
  },
};
