# Changi ⇄ 名古屋 貨物便 Discord 通知

Changi 空港（シンガポール）の貨物便 出発・到着一覧から、**名古屋（NGO / Chubu Centrair）行き・発・経由**の便を
今日・明日・明後日の 3 日分抽出し、Discord へ埋め込み形式で通知します。

```
SIN ⇄ NGO カーゴ便  |  出発 2 件 / 到着 2 件
対象期間: 今日 11/08 (日) 〜 明後日 11/10 (火)
左 = 出発地・出発時刻 / 右 = 到着地・到着時刻

出発便  SIN → NGO
[今日 11/08 (日)]
SIN 04:35    LD 216   NGO 12:17*   出発済 04:37
[明日 11/09 (月)]
SIN 20:35    3S 520   NGO 04:15*+1 定刻

到着便  NGO → SIN
[今日 11/08 (日)]
NGO 18:10-1  LD 215   SIN 01:10    到着済 01:02
```
SIN = シンガポール時間、NGO = 日本時間、`*` = 推定、`+1/-1` = 翌日/前日。
出発便と到着便は別の埋め込みで、左の色バーが 出発=青 / 到着=緑（`.env` の `DISCORD_COLOR_DEP` / `DISCORD_COLOR_ARR` で変更可）。
右端の状態: `出発済/到着済 HH:MM`（実績）、`定刻`、`変更 HH:MM`（変更後予定）、`遅延`、`欠航`。

各便の 2 行目に **名古屋側の時刻（日本時間）** を表示します。
- 出発便 → `NGO着 HH:MM JST (推定)`: シンガポール出発（実績 > 変更後 > 定刻）＋ 標準所要時間で概算
- 到着便 → `NGO発 HH:MM JST`: API の出発地出発時刻をそのまま表示。無い場合は到着 − 所要時間で概算し `(推定)` を付ける
- 所要時間の既定は SIN→NGO 400 分 / NGO→SIN 420 分。`.env` の `FLIGHT_MINUTES_SIN_NGO` / `FLIGHT_MINUTES_NGO_SIN` で調整可

## 取得できる期間について
changiairport.com（日付ピッカー・裏の GraphQL API とも）は **昨日〜明後日の 4 日分**しか提供しておらず、
それより先の日付は "Unable to load" になります（2026-11-08 時点で確認）。
3 日より先の便を知りたい場合は別ソースが必要です（README 末尾「さらに先の便を知るには」参照）。

## 仕組み
出発ページ (`/fly/flight-information/freighter/departures.html`) はブラウザ内の JavaScript が
GraphQL API `https://ca-appsync.lz.changiairport.com/graphql` を呼んで一覧を描画しています。
HTML を解析しても便データは含まれないため、このスクリプトは同じ API を直接呼び出します
（Selenium / Playwright 不要）。API キーはページの HTML に公開埋め込みされているものを
起動時に自動取得します（取得できなければ既定値を使用）。

## セットアップ
```bat
pip install -r requirements.txt
copy .env.example .env
notepad .env        :: DISCORD_WEBHOOK_URL を記入
```
Discord の Webhook URL: サーバー設定 → 連携サービス → ウェブフック → 新しいウェブフック → URL をコピー

## 実行
```bat
python changi_nagoya.py --dry-run     :: 送信せず確認
python changi_nagoya.py               :: 3 日分の該当便を Discord へ送信
python changi_nagoya.py --days 1      :: 今日だけ
python changi_nagoya.py --date 2026-11-09  :: 起点日を指定
python changi_nagoya.py --only-new    :: 前回通知と内容が同じなら送らない（定期実行向け）
python changi_nagoya.py --all         :: 貨物便に加えて旅客便も対象
python changi_nagoya.py --notify-empty:: 0 件でも通知
python changi_nagoya.py --dump all.json :: 取得した全便を JSON 保存（デバッグ）
```

## 定期実行（Windows タスクスケジューラ）
`run_notify.bat` を 1 時間ごと等に登録してください。`--only-new` 付きなので、
便の追加や時刻・状態の変更があったときだけ Discord に流れます（状態は `notified_state.json` に保存）。

## 注意
- サイト側の API キーや GraphQL スキーマが変わると動かなくなることがあります。
  その場合は出発ページのソースで `appSyncApiKey` を探して `.env` の `CHANGI_API_KEY` に設定してください。
- 時刻はシンガポール時間 (SGT) と日本時間 (JST) を併記します。

## さらに先の便を知るには（Changi 側では不可）
| 候補 | 概要 | 先読み範囲 | 備考 |
|---|---|---|---|
| 中部国際空港 (Centrair) 貨物便スケジュール | 空港公式の貨物フライトスケジュール（季節ダイヤ） | シーズン全体 | PDF/HTML 表を解析する必要あり。実績・遅延は無い |
| 各航空会社の貨物スケジュール検索 | SQ Cargo / Cargolux / FedEx / Air Hong Kong 等の公式 | 数週間〜 | 会社ごとに形式が違う |
| AeroDataBox API (RapidAPI) | 空港別の発着スケジュール API、無料枠あり | 数日〜 | NGO/SIN 両方の空港で取得可。貨物便も含まれるが網羅性は要確認 |
| FlightAware AeroAPI / Flightradar24 API | 商用 API | 数日〜 | 有料。精度は高い |

当スクリプトは Changi の公式データ（実績・変更・欠航がリアルタイム）を使う前提なので、
先の予定を足したい場合は上記のいずれかを「予定枠」として追加する構成が現実的です。
