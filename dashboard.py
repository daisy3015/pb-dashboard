#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
파마브로스 대시보드 — 노션 실시간 서버
=====================================
페이지를 열 때마다 노션 API 를 직접 호출해 최신 데이터로 대시보드를 그립니다.
build.py 로 index.html 을 만들어 깃허브에 올리는 과정이 필요 없습니다.

실행 (Windows PowerShell):
    $env:NOTION_TOKEN='ntn_...'             # 노션 인테그레이션 토큰
    python dashboard.py                     # → http://127.0.0.1:8000

옵션:
    --port 8000        포트
    --host 127.0.0.1   0.0.0.0 으로 두면 같은 와이파이의 다른 기기도 접속 가능
    --ttl 60           노션 재조회 최소 간격(초). 새로고침 연타로 API 를 때리지 않게 함
    --export index.html   서버를 띄우지 않고 정적 파일 한 번만 생성 (build.py 대체)
    --once             한 번 조회해서 요약만 출력하고 종료 (연결 점검용)

왜 브라우저가 노션을 직접 못 부르는가
------------------------------------
api.notion.com 은 CORS 를 허용하지 않아 브라우저에서의 직접 호출이 차단됩니다.
그리고 토큰을 페이지에 넣으면 공개 저장소에 그대로 노출됩니다.
그래서 이 스크립트가 중간에서 대신 호출합니다 — 토큰은 서버 쪽에만 남습니다.

UI 는 건드리지 않습니다 (build/template.html 그대로 사용).
시트·발주 데이터는 기존 build/sheet.json · build/orders.json 을 그대로 읽습니다.
"""
import argparse
import base64
import hmac
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"
KST = timezone(timedelta(hours=9))

# build.py 의 변환·매칭 로직을 그대로 재사용한다 (규칙을 두 벌 유지하지 않기 위해)
sys.path.insert(0, str(BUILD))
try:
    import build as B
except ImportError as e:  # pragma: no cover
    sys.exit(f"중단: build/build.py 를 불러올 수 없습니다 — {e}")

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION_NEW = "2025-09-03"   # data source 기반 (현행)
NOTION_VERSION_OLD = "2022-06-28"   # database 기반 (구버전 폴백)

# ── 안전장치 (build.py 와 동일한 취지) ─────────────────────────
MIN_TOTAL_RATIO = 0.80   # 총 항목이 직전 대비 이 비율 미만이면 새 데이터를 쓰지 않음
MIN_NOTION_RATIO = 0.50
NOTION_GUARD_FLOOR = 4


def log(msg):
    print(f"[{datetime.now(KST):%H:%M:%S}] {msg}", flush=True)


def load_json(path, what, default=None):
    """build.py 의 load_json 은 실패 시 프로세스를 죽인다. 서버에서는 곤란하므로 따로 둔다."""
    p = Path(path)
    if not p.exists():
        if default is not None:
            return default
        raise RuntimeError(f"{what} 파일이 없습니다 — {p}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{what} 파일이 올바른 JSON 이 아닙니다 — {p} ({e})")


# ══════════════════════════════════════════════════════════════
#  노션 API
# ══════════════════════════════════════════════════════════════
class NotionError(RuntimeError):
    pass


def _request(url, token, version, payload=None, timeout=30):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST" if payload is not None else "GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": version,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise NotionError(f"HTTP {e.code} — {detail}")
    except urllib.error.URLError as e:
        raise NotionError(f"연결 실패 — {e.reason}")


def fetch_pages(token, source_id):
    """데이터소스(또는 데이터베이스) 전체 페이지를 커서를 따라가며 모두 가져온다.

    2025-09-03 부터 database 아래에 data source 개념이 생겼다.
    새 엔드포인트를 먼저 시도하고, 404/400 이면 구버전 엔드포인트로 넘어간다.
    """
    attempts = [
        (f"{NOTION_API}/data_sources/{source_id}/query", NOTION_VERSION_NEW),
        (f"{NOTION_API}/databases/{source_id}/query", NOTION_VERSION_OLD),
    ]
    last_err = None
    for url, version in attempts:
        try:
            pages, cursor = [], None
            while True:
                payload = {"page_size": 100}
                if cursor:
                    payload["start_cursor"] = cursor
                data = _request(url, token, version, payload)
                pages.extend(data.get("results", []))
                if not data.get("has_more"):
                    break
                cursor = data.get("next_cursor")
                if not cursor:
                    break
            log(f"노션 조회 성공 — {len(pages)}건 (API {version})")
            return pages
        except NotionError as e:
            last_err = e
            if "HTTP 404" in str(e) or "HTTP 400" in str(e):
                continue   # 다음 엔드포인트로
            raise
    raise NotionError(f"노션 조회 실패 — {last_err}")


# ══════════════════════════════════════════════════════════════
#  노션 property → build.py 가 기대하는 납작한 행(row)
#  (기존 SQL 조회 결과와 같은 모양: "이름", "Status 1",
#   "date:입고 목표일:start", "담당자"=["user://..."] 등)
# ══════════════════════════════════════════════════════════════
def prop_value(p):
    t = p.get("type")
    if t in ("title", "rich_text"):
        return "".join(x.get("plain_text", "") for x in (p.get(t) or [])) or None
    if t in ("select", "status"):
        return (p.get(t) or {}).get("name")
    if t == "multi_select":
        return [o.get("name") for o in (p.get("multi_select") or []) if o.get("name")]
    if t == "people":
        return [f"user://{u['id']}" for u in (p.get("people") or []) if u.get("id")]
    if t == "date":
        return p.get("date")            # {"start":..., "end":...} 또는 None
    if t == "relation":
        return [r.get("id") for r in (p.get("relation") or [])]
    if t == "files":
        return [f.get("name") for f in (p.get("files") or [])]
    if t == "formula":
        f = p.get("formula") or {}
        return f.get(f.get("type"))
    if t == "rollup":
        r = p.get("rollup") or {}
        if r.get("type") == "array":
            return [prop_value(x) for x in (r.get("array") or [])]
        return r.get(r.get("type"))
    if t == "unique_id":
        u = p.get("unique_id") or {}
        pre = u.get("prefix")
        return f"{pre}-{u.get('number')}" if pre else u.get("number")
    return p.get(t)   # number, checkbox, url, email, phone_number, created_time, ...


def page_to_row(page):
    row = {}
    for name, p in (page.get("properties") or {}).items():
        v = prop_value(p)
        if p.get("type") == "date":
            # 날짜는 build.py 가 "date:<이름>:start" 로 읽는다.
            # 값이 비어 있어도 키는 항상 만들어 둔다 (조회 결과 모양을 일정하게)
            v = v or {}
            row[f"date:{name}:start"] = v.get("start")
            row[f"date:{name}:end"] = v.get("end")
            row[name] = v.get("start")
        else:
            row[name] = v

    pid = (page.get("id") or "").replace("-", "")
    row["url"] = f"https://app.notion.com/{pid}" if pid else page.get("url")
    row["createdTime"] = page.get("created_time")
    if not row.get("최근 편집일"):
        row["최근 편집일"] = page.get("last_edited_time")
    return row


# ══════════════════════════════════════════════════════════════
#  DB 조립 — build.py main() 의 조립부와 동일한 규칙
# ══════════════════════════════════════════════════════════════
def assemble(rows):
    cfg = load_json(BUILD / "config.json", "설정(config.json)")
    sheet = load_json(BUILD / "sheet.json", "시트 스냅샷(sheet.json)")
    orders = load_json(BUILD / "orders.json", "발주 스냅샷(orders.json)")
    carry = load_json(BUILD / "carry.json", "보존값(carry.json)", {})
    order_notes = load_json(BUILD / "order_notes.json", "발주 비고(order_notes.json)", {})
    owners_map = load_json(BUILD / "owners.json", "담당자 매핑(owners.json)", {})

    warn = []
    notion_items = [x for x in (B.notion_to_item(r, owners_map, carry, warn) for r in rows) if x]

    # 노션 ↔ 시트 매칭 (정규화 후 완전일치 또는 노션이름 ⊇ 시트이름, 유일할 때만)
    cand = [(B.norm(r["name"]), r) for r in sheet if len(B.norm(r["name"])) >= B.MATCH_MIN_LEN]
    sheet_by_key = {r["key"]: dict(r) for r in sheet}
    matched_keys, standalone = set(), []
    for n in notion_items:
        nn = B.norm(n["name"])
        hits = [r for cn, r in cand if cn == nn or cn in nn]
        hits = [r for r in hits if r["key"] not in matched_keys]
        if len(hits) == 1:
            r = sheet_by_key[hits[0]["key"]]
            r.update(src="both", notionUrl=n["notionUrl"], edited=n["edited"],
                     created=n["created"], owners=n["owners"], devName=n["name"])
            matched_keys.add(r["key"])
        else:
            if len(hits) > 1:
                warn.append(f"노션 항목 '{n['name']}' 이 시트 여러 건과 매칭 "
                            f"({', '.join(h['key'] for h in hits)}) — 별도 항목으로 처리")
            standalone.append(n)

    for r in sheet_by_key.values():
        r.setdefault("src", "sheet")

    items = [B.normalize_item(sheet_by_key[r["key"]]) for r in sheet] + \
            [B.normalize_item(x) for x in sorted(standalone, key=lambda x: x["key"])]

    B.fill_missing_order_matches(orders, sheet, warn)
    B.apply_order_notes(orders, order_notes, warn)
    B.attach_po(items, orders)

    return {
        "sheetTitle": cfg.get("sheetTitle"), "sheetUrl": cfg.get("sheetUrl"),
        "notionTitle": cfg.get("notionTitle"), "notionUrl": cfg.get("notionUrl"),
        "process": cfg.get("process") or [],
        "synced_at": datetime.now(KST).isoformat(timespec="seconds"),
        "items": items, "orders": orders,
        "slack": cfg.get("slack") or {}, "gmail": cfg.get("gmail") or {},
    }, warn


def guard(db, prev_items):
    """새 데이터가 직전보다 급감했으면 거부 사유를 돌려준다 (None 이면 통과)."""
    items = db["items"]
    if not items:
        return "노션 조회 결과가 0건입니다"
    if not prev_items:
        return None
    pt, ct = len(prev_items), len(items)
    if ct < pt * MIN_TOTAL_RATIO:
        return f"총 항목이 급감했습니다 ({pt}건 → {ct}건, {MIN_TOTAL_RATIO:.0%} 미만)"
    pn = sum(1 for x in prev_items if x.get("src") in ("notion", "both"))
    cn = sum(1 for x in items if x.get("src") in ("notion", "both"))
    if pn >= NOTION_GUARD_FLOOR and cn < pn * MIN_NOTION_RATIO:
        return f"노션 항목이 급감했습니다 ({pn}건 → {cn}건, {MIN_NOTION_RATIO:.0%} 미만)"
    return None


# ══════════════════════════════════════════════════════════════
#  렌더링
# ══════════════════════════════════════════════════════════════
def render(db, banner=None):
    tpl_path = BUILD / "template.html"
    tpl = tpl_path.read_text(encoding="utf-8")
    if tpl.count("__DB_JSON__") != 1:
        raise RuntimeError(f"템플릿의 __DB_JSON__ 자리표시자가 1개가 아닙니다 "
                           f"({tpl.count('__DB_JSON__')}개) — template.html 이 손상됐습니다")
    blob = (json.dumps(db, ensure_ascii=False, separators=(",", ":"))
            .replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))
    html = tpl.replace("__DB_JSON__", blob)
    if banner:
        safe = banner.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        chip = (f'<div style="position:fixed;left:0;right:0;top:0;z-index:99999;'
                f'background:#b45309;color:#fff;font:13px/1.5 system-ui,sans-serif;'
                f'padding:7px 14px;text-align:center">&#9888; {safe}</div>')
        # 치환문자열의 백슬래시 해석을 피하려고 lambda 를 쓴다
        html = re.sub(r"<body[^>]*>", lambda m: m.group(0) + chip, html, count=1)
    return html


# ══════════════════════════════════════════════════════════════
#  상태 (마지막으로 성공한 데이터를 붙들고 있는다)
# ══════════════════════════════════════════════════════════════
class State:
    def __init__(self, token, source_id, ttl):
        self.token = token
        self.source_id = source_id
        self.ttl = ttl
        self.lock = threading.Lock()
        self.db = None          # 마지막 성공 데이터
        self.warn = []
        # None = 아직 한 번도 조회 안 함. 0.0 을 쓰면 안 된다 —
        # time.monotonic() 은 부팅 후 경과초라 값이 작을 때 "방금 조회함"으로
        # 오판해 노션을 부르지 않고 옛 스냅샷을 내보내게 된다.
        self.fetched_at = None
        self.banner = None

        # 지난 실행 결과를 시작값으로 (build/data.json — build.py 와 같은 파일)
        try:
            prev = load_json(BUILD / "data.json", "직전 스냅샷", None)
            if prev and prev.get("items"):
                self.db = prev
                log(f"직전 스냅샷 불러옴 — {len(prev['items'])}건 "
                    f"(동기화 {prev.get('synced_at')})")
        except RuntimeError:
            pass

    def get(self, force=False):
        with self.lock:
            fresh = (self.fetched_at is not None
                     and (time.monotonic() - self.fetched_at) < self.ttl)
            if self.db is not None and fresh and not force:
                return self.db, self.banner

            try:
                rows = [page_to_row(p) for p in fetch_pages(self.token, self.source_id)]
                db, warn = assemble(rows)
            except (NotionError, RuntimeError) as e:
                log(f"조회 실패: {e}")
                if self.db is None:
                    raise
                self.banner = (f"노션을 불러오지 못해 마지막 데이터를 보여주고 있습니다 "
                               f"(동기화 {self.db.get('synced_at')}) — {e}")
                return self.db, self.banner

            reason = guard(db, (self.db or {}).get("items") or [])
            if reason:
                log(f"안전장치 작동: {reason} — 새 데이터를 쓰지 않습니다")
                self.fetched_at = time.monotonic()
                if self.db is None:
                    raise RuntimeError(f"중단: {reason}")
                self.banner = (f"{reason} — 마지막 정상 데이터를 보여주고 있습니다 "
                               f"(동기화 {self.db.get('synced_at')})")
                return self.db, self.banner

            self.db, self.warn, self.banner = db, warn, None
            self.fetched_at = time.monotonic()
            (BUILD / "data.json").write_text(
                json.dumps(db, ensure_ascii=False, indent=1), encoding="utf-8")
            n_notion = sum(1 for x in db["items"] if x["src"] in ("notion", "both"))
            log(f"갱신 완료 — 총 {len(db['items'])}건 (노션 {n_notion}) / 발주 {len(db['orders'])}건")
            for w in dict.fromkeys(warn):
                log(f"  ! {w}")
            return self.db, self.banner


# ══════════════════════════════════════════════════════════════
#  HTTP
# ══════════════════════════════════════════════════════════════
def make_handler(state, auth):
    """auth = (user, password) 또는 None(인증 없음 — 로컬 전용)"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "pb-dashboard"

        def _send(self, code, body, ctype, extra=None):
            raw = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            # 검색엔진 색인·외부 삽입 차단
            self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(raw)

        def _authorized(self):
            """Basic 인증. 사용자·비밀번호 모두 상수시간 비교한다."""
            if auth is None:
                return True
            head = self.headers.get("Authorization", "")
            if not head.startswith("Basic "):
                return False
            try:
                got = base64.b64decode(head[6:]).decode("utf-8")
            except Exception:                            # noqa: BLE001
                return False
            user, _, pw = got.partition(":")
            ok_u = hmac.compare_digest(user, auth[0])
            ok_p = hmac.compare_digest(pw, auth[1])
            return ok_u and ok_p

        def _challenge(self):
            self._send(401, "인증이 필요합니다", "text/plain",
                       {"WWW-Authenticate": 'Basic realm="pb-dashboard", charset="UTF-8"'})

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            path, _, query = self.path.partition("?")
            force = "fresh=1" in query
            authed = self._authorized()

            # /healthz 는 인증 없이도 200 을 돌려준다 (배포 플랫폼 헬스체크용).
            # 단, 인증 전에는 내용물을 알려주지 않는다.
            if path == "/healthz":
                try:
                    db, banner = state.get()
                except Exception as e:                   # noqa: BLE001
                    self._send(503, json.dumps({"ok": False, "error": str(e)},
                                               ensure_ascii=False), "application/json")
                    return
                body = {"ok": banner is None}
                if authed:
                    body.update(banner=banner, items=len(db["items"]),
                                orders=len(db["orders"]), synced_at=db["synced_at"],
                                warnings=list(dict.fromkeys(state.warn)))
                self._send(200, json.dumps(body, ensure_ascii=False), "application/json")
                return

            if not authed:
                self._challenge()
                return

            try:
                if path in ("/", "/index.html"):
                    db, banner = state.get(force=force)
                    self._send(200, render(db, banner), "text/html")
                elif path == "/api/data":
                    db, _ = state.get(force=force)
                    self._send(200, json.dumps(db, ensure_ascii=False), "application/json")
                elif path == "/robots.txt":
                    self._send(200, "User-agent: *\nDisallow: /\n", "text/plain")
                else:
                    self._send(404, "not found", "text/plain")
            except Exception as e:                      # noqa: BLE001
                log(f"요청 처리 실패: {e}")
                self._send(500, f"오류: {e}", "text/plain")

        def log_message(self, *a):                      # 기본 액세스 로그 끄기
            pass

    return Handler


# ══════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(description="파마브로스 대시보드 — 노션 실시간 서버")
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--insecure", action="store_true",
                    help="비밀번호 없이 외부에 여는 것을 강제 허용 (권장하지 않음)")
    ap.add_argument("--ttl", type=int, default=60, help="노션 재조회 최소 간격(초)")
    ap.add_argument("--source", default=None, help="노션 data source id (기본: config.json / 환경변수)")
    ap.add_argument("--export", metavar="PATH", default=None,
                    help="서버 대신 정적 HTML 한 번만 생성 (build.py 대체)")
    ap.add_argument("--once", action="store_true", help="한 번 조회해 요약만 출력")
    args = ap.parse_args()

    token = os.environ.get("NOTION_TOKEN") or os.environ.get("NOTION_API_KEY")
    if not token:
        sys.exit("중단: 환경변수 NOTION_TOKEN 이 없습니다.\n"
                 "  https://www.notion.so/profile/integrations 에서 인테그레이션을 만들고\n"
                 "  대상 데이터베이스에 연결(Connections)한 뒤:\n"
                 "      $env:NOTION_TOKEN='ntn_...'")

    cfg = load_json(BUILD / "config.json", "설정(config.json)", {})
    source_id = (args.source
                 or os.environ.get("NOTION_DATA_SOURCE_ID")
                 or cfg.get("notionDataSourceId")
                 or "b128c405-e301-82e9-aa21-87a5593d8393")

    state = State(token, source_id, ttl=0 if (args.export or args.once) else args.ttl)

    if args.export or args.once:
        db, banner = state.get(force=True)
        if banner:
            log(f"⚠ {banner}")
        if args.export:
            Path(args.export).write_text(render(db), encoding="utf-8")
            log(f"생성 완료 — {args.export}")
        n_notion = sum(1 for x in db["items"] if x["src"] in ("notion", "both"))
        print(f"\n총 항목 {len(db['items'])}건 (시트 {len(db['items']) - n_notion} / 노션 {n_notion})")
        print(f"발주 {len(db['orders'])}건 · 동기화 {db['synced_at']}")
        return

    # ── 공개 노출 시 인증 강제 ────────────────────────────────
    # 이 대시보드에는 제조원가·발주금액·단가 협상 내용이 들어 있다.
    # 외부에 열면서 비밀번호가 없으면 시작하지 않는다.
    password = os.environ.get("DASHBOARD_PASSWORD")
    user = os.environ.get("DASHBOARD_USER", "pb")
    is_public = args.host not in ("127.0.0.1", "localhost", "::1")
    auth = (user, password) if password else None

    if is_public and auth is None and not args.insecure:
        sys.exit(
            "중단: 외부에 열려 있는데 DASHBOARD_PASSWORD 가 설정되지 않았습니다.\n"
            "  이 대시보드에는 제조원가·발주금액·단가 협상 내용이 들어 있습니다.\n"
            "  URL 을 아는 사람은 누구나 전부 볼 수 있게 됩니다.\n"
            "      $env:DASHBOARD_PASSWORD='충분히 긴 비밀번호'\n"
            "  (정말 인증 없이 열려면 --insecure)")
    if is_public and auth is None:
        log("!! 경고: 인증 없이 외부에 열려 있습니다 — 아무나 볼 수 있습니다")

    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(state, auth))
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "") else args.host
    log(f"대시보드 실행 중 → http://{shown}:{args.port}")
    log(f"노션 data source {source_id} · 재조회 간격 {args.ttl}초 (강제 갱신: ?fresh=1)")
    log(f"인증: {'Basic (' + user + ')' if auth else '없음 (로컬 전용)'}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("종료합니다")


if __name__ == "__main__":
    main()