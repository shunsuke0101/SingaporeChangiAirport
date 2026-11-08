#!/usr/bin/env python3
# coding: utf-8
# -----------------------------------------------------------------------------------#
# @file           SingaporeChangiAirport/changi_nagoya.py
# @brief          Changi Airport 貨物便 名古屋(NGO)関連便 Discord 通知スクリプト
# @author         Kidera Shunsuke
# @date           2026/11/08
# $Version:       1.00
# $Revision:      2026/11/08
# @note           changiairport.com の GraphQL API から出発・到着便を取得し NGO 関連便を通知
# @attention      プロジェクトのコーディングフォーマット・コメントフォーマット遵守
# @par            History
#                 v1.00 (2026/11/08) - 新規作成
# Copyright (c) 2026. All Rights reserved.
#
# -----------------------------------------------------------------------------------#

"""
========================================================================================
モジュール概要：Changi Airport 貨物便 名古屋(NGO)関連便 Discord 通知スクリプト
========================================================================================
Changi Airport（シンガポール）貨物便の出発・到着情報から、名古屋（NGO）行き／発／経由の便を
今日・明日・明後日の 3 日分取得して Discord に通知します。
（changiairport.com は「昨日〜明後日」しか提供していないため、これより先は取得できません）

changiairport.com の便一覧ページはブラウザ上で GraphQL API
(https://ca-appsync.lz.changiairport.com/graphql) を呼び出して描画しているため、
HTML をパースする代わりに同じ API を直接呼び出します（JavaScript 実行不要・高速・安定）。

使い方:
    python changi_nagoya.py                 # 3 日分を取得して Discord へ通知
    python changi_nagoya.py --days 1        # 今日だけ
    python changi_nagoya.py --dry-run       # Discord へ送らず標準出力のみ
    python changi_nagoya.py --only-new      # 前回通知から変化がなければ送らない
    python changi_nagoya.py --all           # 旅客便も含めて検索（既定は貨物便のみ）

変化検知:
    前回通知からの変化（実績時刻が入った、予定が変わった、新しい便）は 🔔(変化) / 🆕(新規) の印を付け、
    状態欄に前回値を添えて目立たせます。状態は notified_state.json に保存します（送信成功時に更新）。

設定:
    Discord の Webhook URL は環境変数 DISCORD_WEBHOOK_URL か、同じフォルダの .env に
    DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/... と書いて渡します。
========================================================================================
"""

# 標準ライブラリ
import argparse                                   # コマンドライン引数解析
import datetime as dt                             # 日付・時刻処理
import json                                       # JSON 入出力
import os                                         # 環境変数アクセス
import sys                                        # 標準エラー出力・終了コード
import time                                       # 時間計測・待機
from concurrent.futures import ThreadPoolExecutor  # 並列リクエスト
from pathlib import Path                          # パス操作
from zoneinfo import ZoneInfo                     # タイムゾーン

# 外部ライブラリ
import requests                                   # HTTP 通信


# ======================================================================================#
# ファイル・パス設定
# ======================================================================================#
BASE_DIR = Path(__file__).resolve().parent        # スクリプト配置フォルダ
STATE_FILE = BASE_DIR / "notified_state.json"     # 前回通知状態の保存先

# ======================================================================================#
# Changi Airport API 設定
# ======================================================================================#
GRAPHQL_ENDPOINT = "https://ca-appsync.lz.changiairport.com/graphql"  # GraphQL エンドポイント
# changiairport.com の便一覧ページ HTML (data-props の appSyncApiKey) に埋め込まれている公開キー。
# 既定では .env の CHANGI_API_KEY を優先し、無ければページから自動取得、ダメなら既定値。
FALLBACK_API_KEY = "da2-umfoldhfsnhh7e3zgbtyr3p6um"  # API キー既定値
DEPARTURES_PAGE = "https://www.changiairport.com/en/fly/flight-information/freighter/departures.html"  # 貨物便出発ページ

# ======================================================================================#
# タイムゾーン設定
# ======================================================================================#
SGT = ZoneInfo("Asia/Singapore")                  # シンガポール時間
JST = ZoneInfo("Asia/Tokyo")                      # 日本時間

# ======================================================================================#
# 名古屋判定設定
# ======================================================================================#
# 名古屋判定キーワード（相手空港 / 経由地の名称・コード）
NAGOYA_WORDS = ("nagoya", "chubu", "centrair")    # 空港名称に含まれる語（小文字）
NAGOYA_CODE = "ngo"                               # IATA コード（小文字）

# ======================================================================================#
# 所要時間設定
# ======================================================================================#
# 名古屋側の時刻を推定するための標準所要時間（直行の目安。.env の FLIGHT_MINUTES_SIN_NGO / NGO_SIN で変更可）
FLIGHT_MIN_SIN_NGO = int(os.environ.get("FLIGHT_MINUTES_SIN_NGO", "400"))   # SIN→NGO 約 6h40m（分）
FLIGHT_MIN_NGO_SIN = int(os.environ.get("FLIGHT_MINUTES_NGO_SIN", "420"))   # NGO→SIN 約 7h00m（分）

# ======================================================================================#
# GraphQL クエリ設定
# ======================================================================================#
# getFlights で取得するフィールド定義
FLIGHT_FIELDS = """
  next_token
  flights {
    airline
    airline_details { code name }
    airport
    airport_details { code name country_code }
    via
    via_airport_details { code name country_code }
    flight_number
    flight_status
    flight_type
    nature
    aircraft_type
    terminal
    direction
    origin_dep_date
    origin_dep_time
    origin_dep_country
    scheduled_date
    scheduled_time
    estimated_timestamp
    display_timestamp
    actual_timestamp
    last_updated_timestamp
    status_mapping { listing_status_en }
  }
"""

# HTTP リクエスト用 User-Agent
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


# ======================================================================================#
# 設定読み込み
# ======================================================================================#

# ======================================================================================#
# @function name:   load_dotenv
# @function start
def load_dotenv(path: Path) -> None:
    """
    ----------------------------------------------------------------------
    関数概要：軽量な .env 読み込み（python-dotenv 不要）
    - 既存の環境変数は上書きしない
    - 空行・コメント行・"=" を含まない行は無視
    ----------------------------------------------------------------------
    Args:
        - path -> Path --- .env ファイルのパス
    ----------------------------------------------------------------------
    Returns:
        - なし
    ----------------------------------------------------------------------
    Notes:
        - author         Kidera-Shunsuke
        - revision       v1.00 (2026/11/08) - 新規作成
    ----------------------------------------------------------------------
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# @function name:   load_dotenv
# @function end
# ======================================================================================#


# ======================================================================================#
# @function name:   resolve_api_key
# @function start
def resolve_api_key(session: requests.Session) -> str:
    """
    ----------------------------------------------------------------------
    関数概要：API キーを .env → ページ HTML → 既定値 の順で決定する
    ----------------------------------------------------------------------
    Args:
        - session -> requests.Session --- HTTP セッション
    ----------------------------------------------------------------------
    Returns:
        - api_key -> str --- 決定した API キー
    ----------------------------------------------------------------------
    Notes:
        - author         Kidera-Shunsuke
        - revision       v1.00 (2026/11/08) - 新規作成
    ----------------------------------------------------------------------
    """
    key = os.environ.get("CHANGI_API_KEY")
    if key:
        return key
    try:
        # ページ HTML 内の "appSyncApiKey" 直後の値を抜き出す
        html = session.get(DEPARTURES_PAGE, headers={"user-agent": UA}, timeout=30).text
        i = html.find('"appSyncApiKey"')
        if i >= 0:
            parts = html[i:i + 200].split('"')
            if len(parts) > 3 and parts[3].startswith("da2-"):
                return parts[3]
    except requests.RequestException:
        pass
    return FALLBACK_API_KEY


# @function name:   resolve_api_key
# @function end
# ======================================================================================#


# ======================================================================================#
# 便情報取得
# ======================================================================================#

# ======================================================================================#
# @function name:   build_query
# @function start
def build_query(direction: str, date_str: str, freighter_only: bool, next_token: str | None) -> str:
    """
    ----------------------------------------------------------------------
    関数概要：getFlights の GraphQL クエリ文字列を組み立てる
    ----------------------------------------------------------------------
    Args:
        - direction -> str --- 方向（"dep" / "arr"）
        - date_str -> str --- 対象日（YYYY-MM-DD）
        - freighter_only -> bool --- 貨物便のみ対象にするかどうか
        - next_token -> str | None --- ページング用トークン（先頭ページは None）
    ----------------------------------------------------------------------
    Returns:
        - query -> str --- GraphQL クエリ文字列
    ----------------------------------------------------------------------
    Notes:
        - author         Kidera-Shunsuke
        - revision       v1.00 (2026/11/08) - 新規作成
    ----------------------------------------------------------------------
    """
    args = [f'direction: "{direction}"']
    # 貨物便ページは scheduled_date に "*" を付けて検索している（日付プレフィックス一致）
    args.append(f'scheduled_date: "{date_str}{"*" if freighter_only else ""}"')
    if freighter_only:
        args.append('load_freighter: "true"')
    else:
        args.append('page_size: "100"')
    if next_token:
        args.append(f'next_token: "{next_token}"')
    return "query { getFlights(" + ", ".join(args) + ") {" + FLIGHT_FIELDS + "} }"


# @function name:   build_query
# @function end
# ======================================================================================#


# ======================================================================================#
# @function name:   fetch_flights
# @function start
def fetch_flights(session: requests.Session, headers: dict, direction: str, date_str: str,
                  freighter_only: bool, max_pages: int = 20) -> list[dict]:
    """
    ----------------------------------------------------------------------
    関数概要：指定方向・指定日の便を全ページ取得して返す
    ----------------------------------------------------------------------
    Args:
        - session -> requests.Session --- HTTP セッション
        - headers -> dict --- リクエストヘッダー（API キー等）
        - direction -> str --- 方向（"dep" / "arr"）
        - date_str -> str --- 対象日（YYYY-MM-DD）
        - freighter_only -> bool --- 貨物便のみ対象にするかどうか
        - max_pages -> int --- 取得する最大ページ数（既定 20）
    ----------------------------------------------------------------------
    Returns:
        - flights -> list[dict] --- 取得した便情報のリスト
    ----------------------------------------------------------------------
    Raises:
        - requests.HTTPError --- HTTP ステータスがエラーの場合
        - RuntimeError --- GraphQL レスポンスに errors が含まれる場合
    ----------------------------------------------------------------------
    Notes:
        - author         Kidera-Shunsuke
        - revision       v1.00 (2026/11/08) - 新規作成
    ----------------------------------------------------------------------
    """
    flights: list[dict] = []
    next_token = None
    for _ in range(max_pages):
        payload = {"query": build_query(direction, date_str, freighter_only, next_token)}
        resp = session.post(GRAPHQL_ENDPOINT, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(f"GraphQL error: {json.dumps(data['errors'], ensure_ascii=False)}")
        result = (data.get("data") or {}).get("getFlights") or {}
        page = result.get("flights") or []
        # 方向情報が無い場合は補完
        for f in page:
            f.setdefault("direction", direction)
        flights.extend(page)
        next_token = result.get("next_token")
        if not next_token or not page:
            break
    return flights


# @function name:   fetch_flights
# @function end
# ======================================================================================#


# ======================================================================================#
# @function name:   fetch_all
# @function start
def fetch_all(dates: list[str], freighter_only: bool) -> tuple[list[dict], list[dict]]:
    """
    ----------------------------------------------------------------------
    関数概要：出発・到着 × 日付 を並列取得する（6 リクエストを同時に投げる）
    - 先頭日の取得失敗は例外として送出
    - 2 日目以降の取得失敗は警告を出して省略（サイトは「今日〜明後日」までしか提供していない）
    - 便番号・予定日・予定時刻で重複を除去
    ----------------------------------------------------------------------
    Args:
        - dates -> list[str] --- 対象日のリスト（YYYY-MM-DD）
        - freighter_only -> bool --- 貨物便のみ対象にするかどうか
    ----------------------------------------------------------------------
    Returns:
        - (dep, arr) -> tuple[list[dict], list[dict]] --- 出発便リストと到着便リスト
    ----------------------------------------------------------------------
    Raises:
        - Exception --- 先頭日の取得に失敗した場合（発生した例外をそのまま送出）
    ----------------------------------------------------------------------
    Notes:
        - author         Kidera-Shunsuke
        - revision       v1.00 (2026/11/08) - 新規作成
    ----------------------------------------------------------------------
    """
    session = requests.Session()
    headers = {
        "content-type": "application/json",
        "x-api-key": resolve_api_key(session),
        "origin": "https://www.changiairport.com",
        "referer": DEPARTURES_PAGE,
        "user-agent": UA,
    }
    jobs = [(direction, i, d) for direction in ("dep", "arr") for i, d in enumerate(dates)]

    # 1 ジョブ（方向 × 日付）を実行し、結果または例外を返す
    def run(job):
        direction, i, d = job
        try:
            return direction, i, fetch_flights(session, headers, direction, d, freighter_only), None
        except Exception as e:  # noqa: BLE001
            return direction, i, [], e

    # 並列実行
    with ThreadPoolExecutor(max_workers=min(6, len(jobs))) as ex:
        results = list(ex.map(run, jobs))

    # 方向別に集約（重複除去）
    out = {"dep": [], "arr": []}
    seen = {"dep": set(), "arr": set()}
    for direction, i, page, err in sorted(results, key=lambda r: (r[0], r[1])):
        if err is not None:
            if i == 0:
                raise err
            # サイトは「今日〜明後日」までしか提供していない。それ以降はエラーになるので省略
            print(f"[WARN] {direction} {dates[i]}: 取得できないため省略 ({err})", file=sys.stderr)
            continue
        for f in page:
            key = (f.get("flight_number"), f.get("scheduled_date"), f.get("scheduled_time"))
            if key not in seen[direction]:
                seen[direction].add(key)
                out[direction].append(f)
    return out["dep"], out["arr"]


# @function name:   fetch_all
# @function end
# ======================================================================================#


# ======================================================================================#
# 名古屋判定・整形
# ======================================================================================#

# ======================================================================================#
# @function name:   _hit
# @function start
def _hit(*parts) -> bool:
    """
    ----------------------------------------------------------------------
    関数概要：与えられた文字列群に名古屋を示す語・コードが含まれるか判定する
    ----------------------------------------------------------------------
    Args:
        - *parts --- 判定対象の文字列（None・空は無視）
    ----------------------------------------------------------------------
    Returns:
        - is_hit -> bool --- 名古屋関連ならば True
    ----------------------------------------------------------------------
    Notes:
        - author         Kidera-Shunsuke
        - revision       v1.00 (2026/11/08) - 新規作成
    ----------------------------------------------------------------------
    """
    hay = " ".join(str(p) for p in parts if p).lower()
    toks = set(hay.replace("(", " ").replace(")", " ").replace("/", " ").replace(",", " ").split())
    return any(w in hay for w in NAGOYA_WORDS) or NAGOYA_CODE in toks


# @function name:   _hit
# @function end
# ======================================================================================#


# ======================================================================================#
# @function name:   is_nagoya
# @function start
def is_nagoya(flight: dict) -> bool:
    """
    ----------------------------------------------------------------------
    関数概要：便の相手空港または経由地が名古屋かどうかを判定する
    ----------------------------------------------------------------------
    Args:
        - flight -> dict --- 便情報
    ----------------------------------------------------------------------
    Returns:
        - is_nagoya -> bool --- 名古屋関連便ならば True
    ----------------------------------------------------------------------
    Notes:
        - author         Kidera-Shunsuke
        - revision       v1.00 (2026/11/08) - 新規作成
    ----------------------------------------------------------------------
    """
    dest = flight.get("airport_details") or {}
    via = flight.get("via_airport_details") or {}
    return _hit(flight.get("airport"), dest.get("code"), dest.get("name")) or \
        _hit(flight.get("via"), via.get("code"), via.get("name"))


# @function name:   is_nagoya
# @function end
# ======================================================================================#


# ======================================================================================#
# @function name:   parse_ts
# @function start
def parse_ts(ts: str | None) -> dt.datetime | None:
    """
    ----------------------------------------------------------------------
    関数概要：API のタイムスタンプ文字列を SGT の aware datetime に変換する
    ----------------------------------------------------------------------
    Args:
        - ts -> str | None --- タイムスタンプ文字列（複数フォーマット対応）
    ----------------------------------------------------------------------
    Returns:
        - datetime -> dt.datetime | None --- 変換結果（変換不能・None の場合は None）
    ----------------------------------------------------------------------
    Notes:
        - author         Kidera-Shunsuke
        - revision       v1.00 (2026/11/08) - 新規作成
    ----------------------------------------------------------------------
    """
    if not ts:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return dt.datetime.strptime(ts[:19], fmt).replace(tzinfo=SGT)
        except ValueError:
            continue
    return None


# @function name:   parse_ts
# @function end
# ======================================================================================#


# 曜日表記（weekday() のインデックス順）
WEEKDAY_JA = "月火水木金土日"


# ======================================================================================#
# @function name:   day_label
# @function start
def day_label(date_str: str, today: dt.date) -> str:
    """
    ----------------------------------------------------------------------
    関数概要：日付文字列を相対表記付きのラベルに整形する
    - 例: '今日 11/08 (日)' / '明日 11/09 (月)' / '11/12 (木)'
    ----------------------------------------------------------------------
    Args:
        - date_str -> str --- 対象日（YYYY-MM-DD）
        - today -> dt.date --- 基準日
    ----------------------------------------------------------------------
    Returns:
        - label -> str --- 整形後ラベル（変換不能時は入力をそのまま返す）
    ----------------------------------------------------------------------
    Notes:
        - author         Kidera-Shunsuke
        - revision       v1.00 (2026/11/08) - 新規作成
    ----------------------------------------------------------------------
    """
    try:
        d = dt.date.fromisoformat(date_str[:10])
    except ValueError:
        return date_str
    rel = {-1: "昨日", 0: "今日", 1: "明日", 2: "明後日"}.get((d - today).days)
    base = f"{d:%m/%d} ({WEEKDAY_JA[d.weekday()]})"
    return f"{rel} {base}" if rel else base


# @function name:   day_label
# @function end
# ======================================================================================#


# ======================================================================================#
# @function name:   flight_code
# @function start
def flight_code(flight: dict) -> str:
    """
    ----------------------------------------------------------------------
    関数概要：航空会社コードと便名の間にスペースを入れる
    - 例: 'SQ7434' → 'SQ 7434'
    ----------------------------------------------------------------------
    Args:
        - flight -> dict --- 便情報
    ----------------------------------------------------------------------
    Returns:
        - code -> str --- 整形後の便名
    ----------------------------------------------------------------------
    Notes:
        - author         Kidera-Shunsuke
        - revision       v1.00 (2026/11/08) - 新規作成
    ----------------------------------------------------------------------
    """
    fn = (flight.get("flight_number") or "?").strip()
    al = (flight.get("airline") or "").strip()
    if al and fn.upper().startswith(al.upper()) and len(fn) > len(al):
        return f"{al} {fn[len(al):].strip()}"
    return fn


# @function name:   flight_code
# @function end
# ======================================================================================#


# ======================================================================================#
# @function name:   status_text
# @function start
def status_text(flight: dict, sched_dt: dt.datetime | None) -> str:
    """
    ----------------------------------------------------------------------
    関数概要：便の内部状態文字列を決定する
    - 例: '出発済 04:37' / '定刻' / '変更 05:20' / '遅延' / '欠航'
    - 優先順位: 実績あり > 欠航 > 予定変更 > 遅延 > 出発/到着済 > ゲート系 > 定刻
    ----------------------------------------------------------------------
    Args:
        - flight -> dict --- 便情報
        - sched_dt -> dt.datetime | None --- 予定時刻（SGT）
    ----------------------------------------------------------------------
    Returns:
        - status -> str --- 内部状態文字列
    ----------------------------------------------------------------------
    Notes:
        - author         Kidera-Shunsuke
        - revision       v1.00 (2026/11/08) - 新規作成
    ----------------------------------------------------------------------
    """
    tag = "到着済" if (flight.get("direction") or "dep").lower().startswith("arr") else "出発済"
    actual = parse_ts(flight.get("actual_timestamp"))
    est = parse_ts(flight.get("estimated_timestamp")) or parse_ts(flight.get("display_timestamp"))
    status = ((flight.get("status_mapping") or {}).get("listing_status_en") or flight.get("flight_status") or "").strip()
    s = status.lower()
    if actual:
        return f"{tag} {actual:%H:%M}"
    if "cancel" in s:
        return "欠航"
    if est and sched_dt and est != sched_dt:
        txt = f"変更 {est:%H:%M}"
        if est.date() != sched_dt.date():
            txt += f" ({est:%m/%d})"
        return txt
    if "delay" in s:
        return "遅延"
    if "depart" in s or "landed" in s or "arrived" in s:
        return tag
    if "gate" in s or "boarding" in s:
        return status
    return "定刻"


# @function name:   status_text
# @function end
# ======================================================================================#


# ======================================================================================#
# @function name:   nagoya_time
# @function start
def nagoya_time(flight: dict, sched_dt: dt.datetime | None) -> tuple[dt.datetime | None, bool]:
    """
    ----------------------------------------------------------------------
    関数概要：名古屋側の時刻（JST, aware）と推定かどうかを求める
    - 出発便 → 名古屋到着時刻（標準所要時間を加算、推定）
    - 到着便（名古屋が出発地） → origin_dep_date/time から確定時刻
    - 到着便（上記以外） → 標準所要時間を減算（推定）
    ----------------------------------------------------------------------
    Args:
        - flight -> dict --- 便情報
        - sched_dt -> dt.datetime | None --- 予定時刻（SGT）
    ----------------------------------------------------------------------
    Returns:
        - (ngo_dt, approx) -> tuple[dt.datetime | None, bool] --- 名古屋側時刻と推定フラグ
    ----------------------------------------------------------------------
    Notes:
        - author         Kidera-Shunsuke
        - revision       v1.00 (2026/11/08) - 新規作成
    ----------------------------------------------------------------------
    """
    is_dep = not (flight.get("direction") or "dep").lower().startswith("arr")
    dest = flight.get("airport_details") or {}
    nagoya_is_dest = _hit(dest.get("code") or flight.get("airport"), dest.get("name"))
    actual = parse_ts(flight.get("actual_timestamp"))
    est = parse_ts(flight.get("estimated_timestamp")) or parse_ts(flight.get("display_timestamp"))
    base = actual or est or sched_dt  # 実績 > 変更後予定 > 定刻
    if is_dep:
        return (base + dt.timedelta(minutes=FLIGHT_MIN_SIN_NGO)).astimezone(JST) if base else None, True
    if nagoya_is_dest:
        od, ot = flight.get("origin_dep_date"), flight.get("origin_dep_time")
        if od and ot:
            try:
                return dt.datetime.strptime(f"{od[:10]} {ot[:5]}", "%Y-%m-%d %H:%M").replace(tzinfo=JST), False
            except ValueError:
                pass
    return (base - dt.timedelta(minutes=FLIGHT_MIN_NGO_SIN)).astimezone(JST) if base else None, True


# @function name:   nagoya_time
# @function end
# ======================================================================================#


# ======================================================================================#
# @function name:   _hhmm_rel
# @function start
def _hhmm_rel(t: dt.datetime | None, ref_date: dt.date | None, approx: bool) -> str:
    """
    ----------------------------------------------------------------------
    関数概要：時刻を基準日との日差と推定マーク付きで整形する
    - 例: '12:17*' / '04:15*+1' / '18:10-1'
    ----------------------------------------------------------------------
    Args:
        - t -> dt.datetime | None --- 対象時刻
        - ref_date -> dt.date | None --- 基準日（日差計算用）
        - approx -> bool --- 推定値かどうか（True なら "*" を付ける）
    ----------------------------------------------------------------------
    Returns:
        - text -> str --- 整形後文字列（None の場合は '--:--'）
    ----------------------------------------------------------------------
    Notes:
        - author         Kidera-Shunsuke
        - revision       v1.00 (2026/11/08) - 新規作成
    ----------------------------------------------------------------------
    """
    if t is None:
        return "--:--"
    s = f"{t:%H:%M}" + ("*" if approx else "")
    if ref_date:
        diff = (t.date() - ref_date).days
        if diff:
            s += f"{diff:+d}"
    return s


# @function name:   _hhmm_rel
# @function end
# ======================================================================================#


# ======================================================================================#
# @function name:   fmt_md
# @function start
def fmt_md(d: dt.datetime | dt.date | None) -> str:
    """
    ----------------------------------------------------------------------
    関数概要：日付を '11/09(月)' 形式に整形する
    ----------------------------------------------------------------------
    Args:
        - d -> dt.datetime | dt.date | None --- 対象日付
    ----------------------------------------------------------------------
    Returns:
        - text -> str --- 整形後文字列（None の場合は '--/--'）
    ----------------------------------------------------------------------
    Notes:
        - author         Kidera-Shunsuke
        - revision       v1.00 (2026/11/08) - 新規作成
    ----------------------------------------------------------------------
    """
    if d is None:
        return "--/--"
    return f"{d:%m/%d}({WEEKDAY_JA[d.weekday()]})"


# @function name:   fmt_md
# @function end
# ======================================================================================#


# ======================================================================================#
# @function name:   ngo_time_label
# @function start
def ngo_time_label(txt: str) -> str:
    """
    ----------------------------------------------------------------------
    関数概要：名古屋側時刻文字列から HH:MM 部分だけを取り出す
    - 例: '05:40*+1' → '05:40'
    ----------------------------------------------------------------------
    Args:
        - txt -> str --- _hhmm_rel で整形した時刻文字列
    ----------------------------------------------------------------------
    Returns:
        - label -> str --- HH:MM 部分
    ----------------------------------------------------------------------
    Notes:
        - author         Kidera-Shunsuke
        - revision       v1.00 (2026/11/08) - 新規作成
    ----------------------------------------------------------------------
    """
    return txt[:5]


# @function name:   ngo_time_label
# @function end
# ======================================================================================#


# ======================================================================================#
# @function name:   ngo_time_suffix
# @function start
def ngo_time_suffix(txt: str) -> str:
    """
    ----------------------------------------------------------------------
    関数概要：名古屋側時刻文字列の日差・推定マークを日本語の補足に変換する
    - 例: '05:40*+1' → ' (翌日・推定)'
    ----------------------------------------------------------------------
    Args:
        - txt -> str --- _hhmm_rel で整形した時刻文字列
    ----------------------------------------------------------------------
    Returns:
        - suffix -> str --- 補足文字列（補足なしの場合は空文字）
    ----------------------------------------------------------------------
    Notes:
        - author         Kidera-Shunsuke
        - revision       v1.00 (2026/11/08) - 新規作成
    ----------------------------------------------------------------------
    """
    parts = []
    if "+1" in txt:
        parts.append("翌日")
    elif "-1" in txt:
        parts.append("前日")
    elif "+2" in txt:
        parts.append("翌々日")
    if "*" in txt:
        parts.append("推定")
    return f" ({'・'.join(parts)})" if parts else ""


# @function name:   ngo_time_suffix
# @function end
# ======================================================================================#


# ======================================================================================#
# @function name:   _mins_diff
# @function start
def _mins_diff(a: str, b: str) -> int | None:
    """
    ----------------------------------------------------------------------
    関数概要：2 つの HH:MM の差分（分）を求める
    - 例: '04:37' - '04:35' → +2
    - 日またぎは ±12h 以内で補正
    ----------------------------------------------------------------------
    Args:
        - a -> str --- 時刻（HH:MM）
        - b -> str --- 基準時刻（HH:MM）
    ----------------------------------------------------------------------
    Returns:
        - minutes -> int | None --- a - b の分差（変換不能時は None）
    ----------------------------------------------------------------------
    Notes:
        - author         Kidera-Shunsuke
        - revision       v1.00 (2026/11/08) - 新規作成
    ----------------------------------------------------------------------
    """
    try:
        ha, ma = map(int, a[:5].split(":")); hb, mb = map(int, b[:5].split(":"))
    except ValueError:
        return None
    d = (ha * 60 + ma) - (hb * 60 + mb)
    if d > 720:
        d -= 1440
    elif d < -720:
        d += 1440
    return d


# @function name:   _mins_diff
# @function end
# ======================================================================================#


# ======================================================================================#
# @function name:   pretty_status
# @function start
def pretty_status(status: str, sched_txt: str, is_dep: bool, prev_status: str | None, prev_sched: str | None, kind: str) -> str:
    """
    ----------------------------------------------------------------------
    関数概要：内部状態を見やすい表示行に変換する
    - 入力: '定刻' / '出発済 04:37' / '変更 05:20' / '遅延' / '欠航' / その他
    - 例: ✅ 出発済み 04:37（予定 04:35、+2分）
          🟡 時刻変更 → 05:20（予定 04:35、+45分）
          🟢 定刻どおり
    - 新規・変化がある場合は 2 行目に 🆕 / 🔔 の補足を付ける
    ----------------------------------------------------------------------
    Args:
        - status -> str --- 内部状態文字列（status_text の戻り値）
        - sched_txt -> str --- 予定時刻（HH:MM）
        - is_dep -> bool --- 出発便かどうか
        - prev_status -> str | None --- 前回通知時の状態
        - prev_sched -> str | None --- 前回通知時の予定時刻
        - kind -> str --- 変化種別（"" / "new" / "changed"）
    ----------------------------------------------------------------------
    Returns:
        - line -> str --- 表示用文字列（1〜2 行）
    ----------------------------------------------------------------------
    Notes:
        - author         Kidera-Shunsuke
        - revision       v1.00 (2026/11/08) - 新規作成
    ----------------------------------------------------------------------
    """
    s = status.strip()
    tag = "出発" if is_dep else "到着"

    # 予定時刻との差分を '、+N分' / '、定刻' 形式で返す
    def delta(t: str) -> str:
        d = _mins_diff(t, sched_txt)
        if d is None:
            return ""
        return "、定刻" if d == 0 else f"、{d:+d}分"

    # 状態別の 1 行目
    if s == "定刻":
        line = "🟢 定刻どおり"
    elif s.startswith(("出発済", "到着済")):
        actual = s.split(" ", 1)[1] if " " in s else ""
        line = f"✅ {tag}済み {actual}（予定 {sched_txt}{delta(actual)}）" if actual else f"✅ {tag}済み"
    elif s.startswith("変更"):
        new = s.split(" ", 1)[1] if " " in s else ""
        line = f"🟡 時刻変更 → {new}（予定 {sched_txt}{delta(new)}）" if new else "🟡 時刻変更"
    elif s == "遅延":
        line = "🟠 遅延"
    elif s == "欠航":
        line = "🔴 欠航"
    else:
        line = f"🔵 {s}"

    # 新規・変化の補足（2 行目）
    if kind == "new":
        line += "\n🆕 新しく追加された便"
    elif kind == "changed":
        if prev_sched and prev_sched != sched_txt:
            line += f"\n🔔 前回: 予定 {prev_sched}・{prev_status or '?'}"
        else:
            line += f"\n🔔 前回: {prev_status or '?'}"
    return line


# @function name:   pretty_status
# @function end
# ======================================================================================#


# ======================================================================================#
# @function name:   describe
# @function start
def describe(flight: dict, today: dt.date, prev: dict | None = None) -> tuple[dict, dict, str]:
    """
    ----------------------------------------------------------------------
    関数概要：1 便を Discord 埋め込みフィールド 1 枠に整形する
    - name : 🛫 LD 216 ｜ 今日 11/08 (日)
    - value: SIN → NGO
             `🛫 SIN 11/08(日) 04:35 SGT`
             `🛬 NGO 11/08(日) 12:17 JST (推定)`
             ✅ 出発済み 04:37（予定 04:35、+2分）
    ----------------------------------------------------------------------
    Args:
        - flight -> dict --- 便情報
        - today -> dt.date --- 基準日（相対日付ラベル用）
        - prev -> dict | None --- 前回通知時の署名（無ければ新規扱い）
    ----------------------------------------------------------------------
    Returns:
        - (field, sig, kind) -> tuple[dict, dict, str] --- 埋め込みフィールド、署名、変化種別（"" / "new" / "changed"）
    ----------------------------------------------------------------------
    Notes:
        - author         Kidera-Shunsuke
        - revision       v1.00 (2026/11/08) - 新規作成
    ----------------------------------------------------------------------
    """
    # 予定時刻・状態・名古屋側時刻の算出
    is_dep = not (flight.get("direction") or "dep").lower().startswith("arr")
    sched = f"{flight.get('scheduled_date', '')} {flight.get('scheduled_time', '')}".strip()
    sched_dt = parse_ts(sched)
    sched_txt = f"{sched_dt:%H:%M}" if sched_dt else (flight.get("scheduled_time") or "--:--")
    ref = sched_dt.date() if sched_dt else None
    status = status_text(flight, sched_dt)
    ngo_dt, approx = (None, False) if status == "欠航" else nagoya_time(flight, sched_dt)
    ngo_txt = _hhmm_rel(ngo_dt, ref, approx)

    # 経路表記（経由地の有無・名古屋の位置で分岐）
    dest = flight.get("airport_details") or {}
    dest_code = dest.get("code") or flight.get("airport") or "?"
    via = flight.get("via_airport_details") or {}
    via_code = via.get("code") or flight.get("via")
    ngo_is_dest = _hit(dest_code, dest.get("name"))
    if is_dep:
        route = "SIN → NGO" if not via_code else (f"SIN → NGO → {dest_code}" if not ngo_is_dest else f"SIN → {via_code} → NGO")
    else:
        route = "NGO → SIN" if not via_code else (f"{dest_code} → NGO → SIN" if not ngo_is_dest else f"NGO → {via_code} → SIN")

    # 前回通知との比較（署名 = 予定時刻 + 状態）
    sig = {"sched": sched_txt, "status": status}
    kind = ""
    if prev is None:
        kind = "new"
    elif prev.get("status") != status or prev.get("sched") != sched_txt:
        kind = "changed"
    status_disp = pretty_status(status, sched_txt, is_dep, (prev or {}).get("status"), (prev or {}).get("sched"), kind)

    # フィールド組み立て
    badge = {"new": "🆕 ", "changed": "🔔 "}.get(kind, "")
    icon = "🛫" if is_dep else "🛬"
    d = (flight.get("scheduled_date") or "")[:10]
    name = f"{badge}{icon} {flight_code(flight)} ｜ {day_label(d, today)}"
    sin_line = f"SIN {fmt_md(sched_dt)} {sched_txt} SGT"
    if ngo_dt is None:
        ngo_line = "NGO --/-- --:-- JST"
    else:
        ngo_line = f"NGO {fmt_md(ngo_dt)} {ngo_dt:%H:%M} JST" + (" (推定)" if approx else "")
    if is_dep:
        times = [f"`🛫 {sin_line}`", f"`🛬 {ngo_line}`"]
    else:
        times = [f"`🛫 {ngo_line}`", f"`🛬 {sin_line}`"]
    value = "\n".join([route, *times, status_disp])
    return {"name": name[:256], "value": value[:1024], "inline": False}, sig, kind  # Discord の文字数上限で切り詰め


# @function name:   describe
# @function end
# ======================================================================================#


# ======================================================================================#
# @function name:   flight_key
# @function start
def flight_key(flight: dict) -> str:
    """
    ----------------------------------------------------------------------
    関数概要：前回状態の保存・照合に使う便の一意キーを生成する
    - 形式: '<direction>_<scheduled_date>_<flight_number>'
    ----------------------------------------------------------------------
    Args:
        - flight -> dict --- 便情報
    ----------------------------------------------------------------------
    Returns:
        - key -> str --- 一意キー
    ----------------------------------------------------------------------
    Notes:
        - author         Kidera-Shunsuke
        - revision       v1.00 (2026/11/08) - 新規作成
    ----------------------------------------------------------------------
    """
    return f"{flight.get('direction')}_{flight.get('scheduled_date')}_{flight.get('flight_number')}"


# @function name:   flight_key
# @function end
# ======================================================================================#


# ======================================================================================#
# @function name:   build_fields
# @function start
def build_fields(flights: list[dict], today: dt.date, state: dict, new_state: dict) -> tuple[list[dict], int]:
    """
    ----------------------------------------------------------------------
    関数概要：便ごとの埋め込みフィールド一覧と変化件数を求める
    - new_state に今回の署名を書き込む（副作用）
    ----------------------------------------------------------------------
    Args:
        - flights -> list[dict] --- 便情報リスト
        - today -> dt.date --- 基準日
        - state -> dict --- 前回通知状態（便キー → 署名）
        - new_state -> dict --- 今回の通知状態を書き込む辞書（更新される）
    ----------------------------------------------------------------------
    Returns:
        - (fields, changed) -> tuple[list[dict], int] --- フィールド一覧と変化件数
    ----------------------------------------------------------------------
    Notes:
        - author         Kidera-Shunsuke
        - revision       v1.00 (2026/11/08) - 新規作成
    ----------------------------------------------------------------------
    """
    fields, changed = [], 0
    for f in flights:
        key = flight_key(f)
        field, sig, kind = describe(f, today, state.get(key))
        new_state[key] = sig
        if kind:
            changed += 1
        fields.append(field)
    # 便と便の間に 1 行空ける（最後の便には付けない）
    for fld in fields[:-1]:
        fld["value"] += "\n​"
    return fields, changed


# @function name:   build_fields
# @function end
# ======================================================================================#


# ======================================================================================#
# @function name:   strip_new_badges
# @function start
def strip_new_badges(fields: list[dict]) -> list[dict]:
    """
    ----------------------------------------------------------------------
    関数概要：フィールドから 🆕 の印を取り除く
    - 初回実行時（前回状態なし）は全件が新規になるので印を消す
    ----------------------------------------------------------------------
    Args:
        - fields -> list[dict] --- 埋め込みフィールド一覧
    ----------------------------------------------------------------------
    Returns:
        - fields -> list[dict] --- 印を取り除いた新しいフィールド一覧
    ----------------------------------------------------------------------
    Notes:
        - author         Kidera-Shunsuke
        - revision       v1.00 (2026/11/08) - 新規作成
    ----------------------------------------------------------------------
    """
    return [{**f, "name": f["name"].replace("🆕 ", ""), "value": f["value"].replace("\n🆕 新しく追加された便", "")} for f in fields]


# @function name:   strip_new_badges
# @function end
# ======================================================================================#


# ======================================================================================#
# @function name:   fields_to_text
# @function start
def fields_to_text(fields: list[dict]) -> str:
    """
    ----------------------------------------------------------------------
    関数概要：埋め込みフィールド一覧を標準出力用のテキストに変換する
    ----------------------------------------------------------------------
    Args:
        - fields -> list[dict] --- 埋め込みフィールド一覧
    ----------------------------------------------------------------------
    Returns:
        - text -> str --- 表示用テキスト（0 件の場合は '  該当便なし'）
    ----------------------------------------------------------------------
    Notes:
        - author         Kidera-Shunsuke
        - revision       v1.00 (2026/11/08) - 新規作成
    ----------------------------------------------------------------------
    """
    if not fields:
        return "  該当便なし"
    return "\n".join(f"  {f['name']}\n" + "\n".join("    " + l for l in f["value"].replace("​", "").splitlines()) for f in fields)


# @function name:   fields_to_text
# @function end
# ======================================================================================#


# ======================================================================================#
# Discord 通知
# ======================================================================================#

# ======================================================================================#
# @function name:   _color
# @function start
def _color(env_name: str, default: int) -> int:
    """
    ----------------------------------------------------------------------
    関数概要：環境変数から埋め込みの色（16 進）を取得する
    ----------------------------------------------------------------------
    Args:
        - env_name -> str --- 環境変数名
        - default -> int --- 未設定・不正時の既定値
    ----------------------------------------------------------------------
    Returns:
        - color -> int --- 色コード
    ----------------------------------------------------------------------
    Notes:
        - author         Kidera-Shunsuke
        - revision       v1.00 (2026/11/08) - 新規作成
    ----------------------------------------------------------------------
    """
    v = os.environ.get(env_name, "").strip().lstrip("#")
    try:
        return int(v, 16) if v else default
    except ValueError:
        return default


# @function name:   _color
# @function end
# ======================================================================================#


# ======================================================================================#
# @function name:   _embeds
# @function start
def _embeds(title: str, fields: list[dict], color: int) -> list[dict]:
    """
    ----------------------------------------------------------------------
    関数概要：フィールド一覧を Discord 埋め込みに分割して組み立てる
    - 1 埋め込みあたり 25 フィールド上限のため 24 件ずつ分割
    ----------------------------------------------------------------------
    Args:
        - title -> str --- 埋め込みタイトル
        - fields -> list[dict] --- 埋め込みフィールド一覧
        - color -> int --- 埋め込みの色
    ----------------------------------------------------------------------
    Returns:
        - embeds -> list[dict] --- 埋め込みリスト（0 件の場合は '該当便なし' の 1 件）
    ----------------------------------------------------------------------
    Notes:
        - author         Kidera-Shunsuke
        - revision       v1.00 (2026/11/08) - 新規作成
    ----------------------------------------------------------------------
    """
    if not fields:
        return [{"title": title, "description": "該当便なし", "color": color}]
    out = []
    for i in range(0, len(fields), 24):  # 25 fields/embed 上限
        out.append({"title": title if i == 0 else f"{title} (続き)", "color": color, "fields": fields[i:i + 24]})
    return out


# @function name:   _embeds
# @function end
# ======================================================================================#


# ======================================================================================#
# @function name:   post_discord
# @function start
def post_discord(webhook: str, title: str, dep_fields: list[dict], arr_fields: list[dict], period: str,
                 n_dep: int, n_arr: int, n_changed: int = 0) -> None:
    """
    ----------------------------------------------------------------------
    関数概要：Discord Webhook へ出発便・到着便の埋め込みを送信する
    - 1 メッセージあたり 10 埋め込み上限のため分割送信
    - 429 (Rate Limit) 時は Retry-After 秒待って 1 回再送
    ----------------------------------------------------------------------
    Args:
        - webhook -> str --- Discord Webhook URL
        - title -> str --- メッセージタイトル
        - dep_fields -> list[dict] --- 出発便フィールド一覧
        - arr_fields -> list[dict] --- 到着便フィールド一覧
        - period -> str --- 対象期間の表示文字列
        - n_dep -> int --- 出発便件数
        - n_arr -> int --- 到着便件数
        - n_changed -> int --- 前回からの変化件数（既定 0）
    ----------------------------------------------------------------------
    Returns:
        - なし
    ----------------------------------------------------------------------
    Raises:
        - requests.HTTPError --- 送信に失敗した場合
    ----------------------------------------------------------------------
    Notes:
        - author         Kidera-Shunsuke
        - revision       v1.00 (2026/11/08) - 新規作成
    ----------------------------------------------------------------------
    """
    embeds = _embeds(f"出発便  SIN → NGO  ({n_dep} 件)", dep_fields, _color("DISCORD_COLOR_DEP", 0x3B82F6)) + \
             _embeds(f"到着便  NGO → SIN  ({n_arr} 件)", arr_fields, _color("DISCORD_COLOR_ARR", 0x22C55E))
    embeds[-1]["footer"] = {"text": f"SGT=シンガポール時間 / JST=日本時間 ・ 日付は各空港の現地日付 ・ 推定=標準所要時間からの概算 ・ 🔔=前回通知から変化 ・ 更新 {dt.datetime.now(JST):%m/%d %H:%M} JST"}
    chg = f"  ・ 変化 {n_changed} 件" if n_changed else ""
    for i in range(0, len(embeds), 10):  # 10 embeds/message 上限
        payload = {"embeds": embeds[i:i + 10]}
        if i == 0:
            payload["content"] = f"**{title}**{chg}\n対象期間: {period}"
        r = requests.post(webhook, json=payload, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.headers.get("Retry-After", "2")))
            r = requests.post(webhook, json=payload, timeout=30)
        r.raise_for_status()


# @function name:   post_discord
# @function end
# ======================================================================================#


# ======================================================================================#
# 前回通知状態の管理
# ======================================================================================#

# ======================================================================================#
# @function name:   load_state
# @function start
def load_state() -> dict:
    """
    ----------------------------------------------------------------------
    関数概要：前回通知状態を JSON ファイルから読み込む
    ----------------------------------------------------------------------
    Args:
        - なし
    ----------------------------------------------------------------------
    Returns:
        - state -> dict --- 前回通知状態（ファイル無し・破損時は空辞書）
    ----------------------------------------------------------------------
    Notes:
        - author         Kidera-Shunsuke
        - revision       v1.00 (2026/11/08) - 新規作成
    ----------------------------------------------------------------------
    """
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


# @function name:   load_state
# @function end
# ======================================================================================#


# ======================================================================================#
# @function name:   save_state
# @function start
def save_state(state: dict) -> None:
    """
    ----------------------------------------------------------------------
    関数概要：今回の通知状態を JSON ファイルへ保存する
    ----------------------------------------------------------------------
    Args:
        - state -> dict --- 通知状態（便キー → 署名）
    ----------------------------------------------------------------------
    Returns:
        - なし
    ----------------------------------------------------------------------
    Notes:
        - author         Kidera-Shunsuke
        - revision       v1.00 (2026/11/08) - 新規作成
    ----------------------------------------------------------------------
    """
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# @function name:   save_state
# @function end
# ======================================================================================#


# ======================================================================================#
# メイン処理
# ======================================================================================#

# ======================================================================================#
# @function name:   main
# @function start
def main() -> int:
    """
    ----------------------------------------------------------------------
    関数概要：Changi 貨物便から名古屋関連便を取得して Discord へ通知するメイン処理
    - .env 読み込み・引数解析
    - 便情報取得・名古屋判定・整形
    - 標準出力表示
    - Discord 送信（--dry-run / --only-new / --notify-empty で制御）
    - 送信成功時に通知状態を保存
    ----------------------------------------------------------------------
    Args:
        - なし
    ----------------------------------------------------------------------
    Returns:
        - exit_code -> int --- 終了コード
            0: 正常終了（スキップ含む）
            2: 便情報取得失敗
            3: Webhook URL 未設定
            4: Discord 送信失敗
    ----------------------------------------------------------------------
    Notes:
        - author         Kidera-Shunsuke
        - revision       v1.00 (2026/11/08) - 新規作成
    ----------------------------------------------------------------------
    """
    # .env 読み込みと所要時間設定の再評価（.env の値を反映）
    load_dotenv(BASE_DIR / ".env")
    global FLIGHT_MIN_SIN_NGO, FLIGHT_MIN_NGO_SIN
    FLIGHT_MIN_SIN_NGO = int(os.environ.get("FLIGHT_MINUTES_SIN_NGO", FLIGHT_MIN_SIN_NGO))
    FLIGHT_MIN_NGO_SIN = int(os.environ.get("FLIGHT_MINUTES_NGO_SIN", FLIGHT_MIN_NGO_SIN))

    # コマンドライン引数
    p = argparse.ArgumentParser(description="Changi 貨物便から名古屋行き・発・経由便を Discord へ通知")
    p.add_argument("--date", help="起点日 YYYY-MM-DD（既定: シンガポール時間の今日）")
    p.add_argument("--days", type=int, default=3, help="取得する日数（既定 3 = 今日・明日・明後日。サイト仕様上これが上限）")
    p.add_argument("--all", action="store_true", help="貨物便だけでなく旅客便も対象にする")
    p.add_argument("--dry-run", action="store_true", help="Discord へ送信せず表示のみ")
    p.add_argument("--only-new", action="store_true", help="前回通知から変化（実績・予定・新規）が無ければ送らない")
    p.add_argument("--notify-empty", action="store_true", help="該当便が 0 件でも Discord へ通知する")
    p.add_argument("--webhook", help="Discord Webhook URL（環境変数 DISCORD_WEBHOOK_URL より優先）")
    p.add_argument("--dump", help="取得した全便を JSON で保存するパス（デバッグ用）")
    p.add_argument("--reset-state", action="store_true", help="前回状態を無視する（全件を新規扱い）")
    a = p.parse_args()

    # 対象日の決定
    today = dt.date.fromisoformat(a.date) if a.date else dt.datetime.now(SGT).date()
    dates = [(today + dt.timedelta(days=i)).isoformat() for i in range(max(1, a.days))]
    freighter_only = not a.all

    # 便情報取得
    t0 = time.perf_counter()
    try:
        deps, arrs = fetch_all(dates, freighter_only)
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] 取得失敗: {e}", file=sys.stderr)
        return 2
    print(f"取得 {time.perf_counter() - t0:.1f}s")

    # デバッグ用ダンプ
    if a.dump:
        Path(a.dump).write_text(json.dumps({"dep": deps, "arr": arrs}, ensure_ascii=False, indent=2), encoding="utf-8")

    # 名古屋関連便の抽出・ソート
    sort_key = lambda f: (f.get("scheduled_date", ""), f.get("scheduled_time", ""))  # noqa: E731
    dep_m = sorted((f for f in deps if is_nagoya(f)), key=sort_key)
    arr_m = sorted((f for f in arrs if is_nagoya(f)), key=sort_key)
    label = "カーゴ" if freighter_only else "全便"
    print(f"{dates[0]}〜{dates[-1]} 出発 {len(deps)} 件 / 到着 {len(arrs)} 件 → NGO 関連 出発 {len(dep_m)} / 到着 {len(arr_m)}")

    # 前回状態との比較・フィールド組み立て
    state = {} if a.reset_state else load_state()
    new_state: dict = {}
    dep_fields, c1 = build_fields(dep_m, today, state, new_state)
    arr_fields, c2 = build_fields(arr_m, today, state, new_state)
    n_changed = c1 + c2 if state else 0  # 初回（状態ファイル無し）は全件新規なので数えない
    if not state:
        dep_fields, arr_fields = strip_new_badges(dep_fields), strip_new_badges(arr_fields)
    period = f"{day_label(dates[0], today)} 〜 {day_label(dates[-1], today)}"
    title = f"SIN ⇄ NGO {label}便  |  出発 {len(dep_m)} 件 / 到着 {len(arr_m)} 件"

    # 標準出力表示
    print(title, f"(変化 {n_changed} 件)" if n_changed else "")
    print(f"対象期間: {period}")
    print("[出発便  SIN → NGO]")
    print(fields_to_text(dep_fields))
    print("[到着便  NGO → SIN]")
    print(fields_to_text(arr_fields))

    # 送信スキップ判定
    if a.dry_run:
        return 0
    if not (dep_m or arr_m) and not a.notify_empty:
        print("通知対象なしのため Discord 送信をスキップ（--notify-empty で送信可）")
        return 0
    if a.only_new and state and n_changed == 0:
        print("前回通知から変化なしのためスキップ")
        return 0

    # Discord 送信
    webhook = a.webhook or os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        print("[ERROR] DISCORD_WEBHOOK_URL が未設定です（.env か環境変数で指定）", file=sys.stderr)
        return 3
    try:
        post_discord(webhook, title, dep_fields, arr_fields, period, len(dep_m), len(arr_m), n_changed)
        print("Discord へ送信しました")
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] Discord 送信失敗: {e}", file=sys.stderr)
        return 4
    save_state(new_state)  # 送信できたときだけ状態を更新
    return 0


# @function name:   main
# @function end
# ======================================================================================#


if __name__ == "__main__":
    # 独立実行時の処理
    sys.exit(main())
