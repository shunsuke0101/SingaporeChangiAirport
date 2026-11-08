#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Changi Airport（シンガポール）貨物便の出発・到着情報から、名古屋（NGO）行き／発／経由の便を
今日・明日・明後日の 3 日分取得して Discord に通知するスクリプト。
（changiairport.com は「昨日〜明後日」しか提供していないため、これより先は取得できない）

changiairport.com の便一覧ページはブラウザ上で GraphQL API
(https://ca-appsync.lz.changiairport.com/graphql) を呼び出して描画しているため、
HTML をパースする代わりに同じ API を直接呼び出す（JavaScript 実行不要・高速・安定）。

使い方:
    python changi_nagoya.py                 # 3 日分を取得して Discord へ通知
    python changi_nagoya.py --days 1        # 今日だけ
    python changi_nagoya.py --dry-run       # Discord へ送らず標準出力のみ
    python changi_nagoya.py --only-new      # 前回通知時から変化がなければ送らない
    python changi_nagoya.py --all           # 旅客便も含めて検索（既定は貨物便のみ）

Discord の Webhook URL は環境変数 DISCORD_WEBHOOK_URL か、同じフォルダの .env に
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/... と書いて渡す。
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "notified_state.json"

GRAPHQL_ENDPOINT = "https://ca-appsync.lz.changiairport.com/graphql"
# changiairport.com の便一覧ページ HTML (data-props の appSyncApiKey) に埋め込まれている公開キー。
# 既定では .env の CHANGI_API_KEY を優先し、無ければページから自動取得、ダメなら既定値。
FALLBACK_API_KEY = "da2-umfoldhfsnhh7e3zgbtyr3p6um"
DEPARTURES_PAGE = "https://www.changiairport.com/en/fly/flight-information/freighter/departures.html"

SGT = ZoneInfo("Asia/Singapore")
JST = ZoneInfo("Asia/Tokyo")

# 名古屋判定キーワード（相手空港 / 経由地の名称・コード）
NAGOYA_WORDS = ("nagoya", "chubu", "centrair")
NAGOYA_CODE = "ngo"

# 名古屋側の時刻を推定するための標準所要時間（直行の目安。.env の FLIGHT_MINUTES_SIN_NGO / NGO_SIN で変更可）
FLIGHT_MIN_SIN_NGO = int(os.environ.get("FLIGHT_MINUTES_SIN_NGO", "400"))   # 約 6h40m
FLIGHT_MIN_NGO_SIN = int(os.environ.get("FLIGHT_MINUTES_NGO_SIN", "420"))   # 約 7h00m

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

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


# ---------------------------------------------------------------- 設定
def load_dotenv(path: Path) -> None:
    """軽量な .env 読み込み（python-dotenv 不要）。既存の環境変数は上書きしない。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def resolve_api_key(session: requests.Session) -> str:
    """API キーを .env → ページ HTML → 既定値 の順で決定する。"""
    key = os.environ.get("CHANGI_API_KEY")
    if key:
        return key
    try:
        html = session.get(DEPARTURES_PAGE, headers={"user-agent": UA}, timeout=30).text
        i = html.find('"appSyncApiKey"')
        if i >= 0:
            parts = html[i:i + 200].split('"')
            if len(parts) > 3 and parts[3].startswith("da2-"):
                return parts[3]
    except requests.RequestException:
        pass
    return FALLBACK_API_KEY


# ---------------------------------------------------------------- 取得
def build_query(direction: str, date_str: str, freighter_only: bool, next_token: str | None) -> str:
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


def fetch_flights(session: requests.Session, headers: dict, direction: str, date_str: str,
                  freighter_only: bool, max_pages: int = 20) -> list[dict]:
    """指定方向・指定日の便を全ページ取得して返す。"""
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
        for f in page:
            f.setdefault("direction", direction)
        flights.extend(page)
        next_token = result.get("next_token")
        if not next_token or not page:
            break
        time.sleep(0.3)
    return flights


def fetch_all(dates: list[str], freighter_only: bool) -> tuple[list[dict], list[dict]]:
    session = requests.Session()
    headers = {
        "content-type": "application/json",
        "x-api-key": resolve_api_key(session),
        "origin": "https://www.changiairport.com",
        "referer": DEPARTURES_PAGE,
        "user-agent": UA,
    }
    out = {"dep": [], "arr": []}
    for direction in ("dep", "arr"):
        seen = set()
        for i, d in enumerate(dates):
            try:
                page = fetch_flights(session, headers, direction, d, freighter_only)
            except Exception as e:  # noqa: BLE001
                if i == 0:
                    raise
                # サイトは「今日〜明後日」までしか提供していない。それ以降はエラーになるので打ち切る
                print(f"[WARN] {direction} {d}: 取得できないため以降の日付は省略 ({e})", file=sys.stderr)
                break
            for f in page:
                key = (f.get("flight_number"), f.get("scheduled_date"), f.get("scheduled_time"))
                if key not in seen:
                    seen.add(key)
                    out[direction].append(f)
            time.sleep(0.3)
    return out["dep"], out["arr"]


# ---------------------------------------------------------------- 判定・整形
def _hit(*parts) -> bool:
    hay = " ".join(str(p) for p in parts if p).lower()
    toks = set(hay.replace("(", " ").replace(")", " ").replace("/", " ").replace(",", " ").split())
    return any(w in hay for w in NAGOYA_WORDS) or NAGOYA_CODE in toks


def is_nagoya(flight: dict) -> bool:
    dest = flight.get("airport_details") or {}
    via = flight.get("via_airport_details") or {}
    return _hit(flight.get("airport"), dest.get("code"), dest.get("name")) or \
        _hit(flight.get("via"), via.get("code"), via.get("name"))


def parse_ts(ts: str | None) -> dt.datetime | None:
    if not ts:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return dt.datetime.strptime(ts[:19], fmt).replace(tzinfo=SGT)
        except ValueError:
            continue
    return None


WEEKDAY_JA = "月火水木金土日"


def day_label(date_str: str, today: dt.date) -> str:
    """例: '今日 11/08 (日)' / '明日 11/09 (月)' / '11/12 (木)'"""
    try:
        d = dt.date.fromisoformat(date_str[:10])
    except ValueError:
        return date_str
    rel = {-1: "昨日", 0: "今日", 1: "明日", 2: "明後日"}.get((d - today).days)
    base = f"{d:%m/%d} ({WEEKDAY_JA[d.weekday()]})"
    return f"{rel} {base}" if rel else base


def flight_code(flight: dict) -> str:
    """'SQ7434' → 'SQ 7434' のように航空会社コードと便名の間にスペースを入れる。"""
    fn = (flight.get("flight_number") or "?").strip()
    al = (flight.get("airline") or "").strip()
    if al and fn.upper().startswith(al.upper()) and len(fn) > len(al):
        return f"{al} {fn[len(al):].strip()}"
    return fn


def status_text(flight: dict, sched_dt: dt.datetime | None) -> str:
    """右端に出す状態。例: '出発済 04:37' / '定刻' / '変更 05:20' / '欠航'"""
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


def nagoya_time(flight: dict, sched_dt: dt.datetime | None) -> tuple[dt.datetime | None, bool]:
    """名古屋側の時刻 (JST, aware) と 推定かどうか。出発便→名古屋到着、到着便→名古屋出発。"""
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


def _hhmm_rel(t: dt.datetime | None, ref_date: dt.date | None, approx: bool) -> str:
    """'12:17*' / '04:15*+1' / '18:10-1' のように、基準日との日差と推定マークを付ける。"""
    if t is None:
        return "--:--"
    s = f"{t:%H:%M}" + ("*" if approx else "")
    if ref_date:
        diff = (t.date() - ref_date).days
        if diff:
            s += f"{diff:+d}"
    return s


def describe(flight: dict, today: dt.date) -> str:
    """1 行分（等幅前提）。
    出発: 'SIN 04:35  LD 216    NGO 12:17*     出発済 04:37'
    到着: 'NGO 18:10  LD 215    SIN 01:10      到着済 01:02'
    """
    is_dep = not (flight.get("direction") or "dep").lower().startswith("arr")
    sched = f"{flight.get('scheduled_date', '')} {flight.get('scheduled_time', '')}".strip()
    sched_dt = parse_ts(sched)
    sched_txt = f"{sched_dt:%H:%M}" if sched_dt else (flight.get("scheduled_time") or "--:--")
    ref = sched_dt.date() if sched_dt else None
    status = status_text(flight, sched_dt)
    ngo_dt, approx = (None, False) if status == "欠航" else nagoya_time(flight, sched_dt)
    ngo_txt = _hhmm_rel(ngo_dt, ref, approx)

    dest = flight.get("airport_details") or {}
    dest_code = dest.get("code") or flight.get("airport") or "?"
    via = flight.get("via_airport_details") or {}
    via_code = via.get("code") or flight.get("via")
    note = ""
    if via_code and not _hit(dest_code, dest.get("name")):
        note = f" ({'NGO→' + dest_code if is_dep else dest_code + '→NGO'})"
    elif via_code:
        note = f" (経由{via_code})"

    code = flight_code(flight)
    if is_dep:
        left, right = f"SIN {sched_txt}", f"NGO {ngo_txt}"
    else:
        left, right = f"NGO {ngo_txt}", f"SIN {sched_txt}"
    return f"{left:<12} {code:<8} {right:<12} {status}{note}"


def group_by_day(flights: list[dict], today: dt.date) -> str:
    """日付ごとに見出しを付け、コードブロック（等幅）でまとめる。"""
    if not flights:
        return "該当便なし"
    rows, cur = [], None
    for f in flights:
        d = (f.get("scheduled_date") or "")[:10]
        if d != cur:
            cur = d
            if rows:
                rows.append("")
            rows.append(f"[{day_label(d, today)}]")
        rows.append(describe(f, today))
    txt = "\n".join(rows)
    limit = 4000
    if len(txt) > limit:
        txt = txt[:limit - 2] + "\n…"
    return "```\n" + txt + "\n```"


# ---------------------------------------------------------------- Discord
def _color(env_name: str, default: int) -> int:
    v = os.environ.get(env_name, "").strip().lstrip("#")
    try:
        return int(v, 16) if v else default
    except ValueError:
        return default


def post_discord(webhook: str, title: str, dep_txt: str, arr_txt: str, period: str,
                 n_dep: int, n_arr: int) -> None:
    """出発・到着を別々の埋め込みにして色分けする（1 メッセージに 2 枚）。"""
    footer = {"text": f"SIN=シンガポール時間 / NGO=日本時間 ・ *=推定 ・ +1=翌日 ・ 更新 {dt.datetime.now(JST):%m/%d %H:%M} JST"}
    embeds = [
        {
            "title": f"出発便  SIN → NGO  ({n_dep} 件)",
            "description": dep_txt,
            "color": _color("DISCORD_COLOR_DEP", 0x3B82F6),   # 青
        },
        {
            "title": f"到着便  NGO → SIN  ({n_arr} 件)",
            "description": arr_txt,
            "color": _color("DISCORD_COLOR_ARR", 0x22C55E),   # 緑
            "footer": footer,
        },
    ]
    payload = {
        "content": f"**{title}**\n対象期間: {period}  (左 = 出発地・出発時刻 / 右 = 到着地・到着時刻)",
        "embeds": embeds,
    }
    r = requests.post(webhook, json=payload, timeout=30)
    if r.status_code == 429:
        time.sleep(float(r.headers.get("Retry-After", "2")))
        r = requests.post(webhook, json=payload, timeout=30)
    r.raise_for_status()


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- main
def main() -> int:
    load_dotenv(BASE_DIR / ".env")
    global FLIGHT_MIN_SIN_NGO, FLIGHT_MIN_NGO_SIN
    FLIGHT_MIN_SIN_NGO = int(os.environ.get("FLIGHT_MINUTES_SIN_NGO", FLIGHT_MIN_SIN_NGO))
    FLIGHT_MIN_NGO_SIN = int(os.environ.get("FLIGHT_MINUTES_NGO_SIN", FLIGHT_MIN_NGO_SIN))
    p = argparse.ArgumentParser(description="Changi 貨物便から名古屋行き・発・経由便を Discord へ通知")
    p.add_argument("--date", help="起点日 YYYY-MM-DD（既定: シンガポール時間の今日）")
    p.add_argument("--days", type=int, default=3, help="取得する日数（既定 3 = 今日・明日・明後日。サイト仕様上これが上限）")
    p.add_argument("--all", action="store_true", help="貨物便だけでなく旅客便も対象にする")
    p.add_argument("--dry-run", action="store_true", help="Discord へ送信せず表示のみ")
    p.add_argument("--only-new", action="store_true", help="前回通知時から内容が変わっていなければ送らない")
    p.add_argument("--notify-empty", action="store_true", help="該当便が 0 件でも Discord へ通知する")
    p.add_argument("--webhook", help="Discord Webhook URL（環境変数 DISCORD_WEBHOOK_URL より優先）")
    p.add_argument("--dump", help="取得した全便を JSON で保存するパス（デバッグ用）")
    a = p.parse_args()

    today = dt.date.fromisoformat(a.date) if a.date else dt.datetime.now(SGT).date()
    dates = [(today + dt.timedelta(days=i)).isoformat() for i in range(max(1, a.days))]
    freighter_only = not a.all

    try:
        deps, arrs = fetch_all(dates, freighter_only)
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] 取得失敗: {e}", file=sys.stderr)
        return 2

    if a.dump:
        Path(a.dump).write_text(json.dumps({"dep": deps, "arr": arrs}, ensure_ascii=False, indent=2), encoding="utf-8")

    sort_key = lambda f: (f.get("scheduled_date", ""), f.get("scheduled_time", ""))  # noqa: E731
    dep_m = sorted((f for f in deps if is_nagoya(f)), key=sort_key)
    arr_m = sorted((f for f in arrs if is_nagoya(f)), key=sort_key)
    label = "カーゴ" if freighter_only else "全便"
    print(f"{dates[0]}〜{dates[-1]} 出発 {len(deps)} 件 / 到着 {len(arrs)} 件 → NGO 関連 出発 {len(dep_m)} / 到着 {len(arr_m)}")

    dep_txt = group_by_day(dep_m, today)
    arr_txt = group_by_day(arr_m, today)
    period = f"{day_label(dates[0], today)} 〜 {day_label(dates[-1], today)}"
    title = f"SIN ⇄ NGO {label}便  |  出発 {len(dep_m)} 件 / 到着 {len(arr_m)} 件"

    print(title)
    print(f"対象期間: {period}")
    print("[出発  SIN → NGO]")
    print(dep_txt)
    print("[到着  NGO → SIN]")
    print(arr_txt)

    if a.dry_run:
        return 0
    if not (dep_m or arr_m) and not a.notify_empty:
        print("通知対象なしのため Discord 送信をスキップ（--notify-empty で送信可）")
        return 0

    if a.only_new:
        state = load_state()
        sig = dep_txt + "\n--\n" + arr_txt
        if state.get("last") == sig:
            print("前回通知から変化なしのためスキップ")
            return 0
        save_state({"last": sig, "at": dt.datetime.now(JST).isoformat()})

    webhook = a.webhook or os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        print("[ERROR] DISCORD_WEBHOOK_URL が未設定です（.env か環境変数で指定）", file=sys.stderr)
        return 3
    try:
        post_discord(webhook, title, dep_txt, arr_txt, period, len(dep_m), len(arr_m))
        print("Discord へ送信しました")
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] Discord 送信失敗: {e}", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
