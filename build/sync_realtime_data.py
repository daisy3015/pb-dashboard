#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
슬랙 발주 실시간 수집
====================
슬랙 발주 채널(build/config.json 의 slack.channelId)을 직접 조회해
build/orders.json 에 새 발주를 누적 추가하고, build/build.py 로 index.html 을
재빌드한 뒤 git commit·push 까지 자동으로 수행합니다.

해외 발주(Gmail)는 이 스크립트가 아니라 구글 시트 Apps Script 트리거로
별도 자동화되어 있습니다 — 그 시트를 대시보드에 반영하는 부분은
build/sync_sheet.py(또는 전용 스크립트)에서 처리합니다.

★ 읽어두세요 (build/DEPLOY.md 참고) ─────────────────────────────────────
발주는 금액·수량이 걸린 데이터입니다. 이 스크립트는 사람이 슬랙 원문을
읽고 옮겨 적던 지점을 정규식 자동 파싱으로 대체합니다 — 파싱 실패·오인식
가능성이 있으므로, 제품명/수량/납기일 중 하나라도 확신할 수 없는 건은
`needsReview: true` 로 표시해 화면에 "확인필요" 배지로 남깁니다.
**절대 조용히 버리지 않습니다** — 다만 조용히 자동 반영되는 것도 아니니,
"확인필요" 배지가 붙은 건은 대시보드에서 주기적으로 사람이 확인해야 합니다.

의존성은 표준 라이브러리만 사용합니다 (slack_sdk 대신 슬랙 Web API를
urllib 로 직접 호출 — 이 저장소의 다른 스크립트들과 동일한 방침).

사용법:
    python3 build/sync_realtime_data.py                 # 1회 실행
    python3 build/sync_realtime_data.py --loop           # 반복 실행 (기본 2분 간격)
    python3 build/sync_realtime_data.py --dry-run         # 조회·파싱만, 저장/빌드/커밋 없음
    python3 build/sync_realtime_data.py --no-git          # orders.json/빌드까지만, git push 는 생략

환경변수 (.env.example 참고):
    SLACK_BOT_TOKEN        슬랙 봇 토큰 (xoxb-...). 채널에 봇 초대 필요
                           스코프: channels:history(또는 groups:history), channels:read, users:read
    GITHUB_PUSH_TOKEN      git push 용 GitHub PAT (Contents: read/write)
                           — 배포 환경(Render/Fly)에는 기본적으로 push 권한이 없습니다
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BUILD = Path(__file__).resolve().parent
ROOT = BUILD.parent
KST = timezone(timedelta(hours=9))


def log(msg):
    print(f"[{datetime.now(KST):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def load_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def save_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def load_dotenv(path=None):
    """python-dotenv 없이 .env 를 읽어 os.environ 에 없는 값만 채운다 (로컬 테스트용).

    배포 환경(Render/Fly)에서는 플랫폼이 이미 실제 환경변수를 주입하므로
    이 함수는 아무 효과가 없다 (os.environ.setdefault 라 기존 값을 덮지 않음).
    """
    p = Path(path) if path else ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ══════════════════════════════════════════════════════════════
#  슬랙 — Web API 직접 호출 (urllib, slack_sdk 미사용)
# ══════════════════════════════════════════════════════════════
SLACK_API = "https://slack.com/api"


def slack_call(method, token, params):
    url = f"{SLACK_API}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        body = json.loads(r.read().decode("utf-8"))
    if not body.get("ok"):
        raise RuntimeError(f"슬랙 API {method} 실패 — {body.get('error')}")
    return body


_slack_user_cache = {}


def slack_user_name(token, uid):
    if not uid:
        return None
    if uid in _slack_user_cache:
        return _slack_user_cache[uid]
    try:
        body = slack_call("users.info", token, {"user": uid})
        u = body.get("user") or {}
        name = u.get("real_name") or u.get("name") or uid
    except Exception:                                    # noqa: BLE001
        name = uid
    _slack_user_cache[uid] = name
    return name


BULLET_RE = re.compile(r"^[•\-\*]\s*([^:：]+)[:：]\s*(.+)$")
BRACKET_RE = re.compile(r"\[([^\]]+)\]")
TRAILING_KIND_RE = re.compile(r"\s*(재발주|추가발주|추가|발주)?\s*(의\s*건)?\s*$")


def parse_slack_message(msg, channel_url, token):
    """build/README.md 의 '슬랙 메시지 → JSON 추출 규칙' 을 정규식으로 재현한다."""
    ts = msg.get("ts")
    text = msg.get("text") or ""
    if not ts or not text.strip():
        return None

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    title = lines[0] if lines else ""
    fields = {}
    for line in lines[1:]:
        m = BULLET_RE.match(line)
        if m:
            fields[m.group(1).strip()] = m.group(2).strip()

    if "재발주" in title:
        kind = "재발주"
    elif "추가" in title:
        kind = "추가발주"
    else:
        kind = "발주"

    product = fields.get("제품명")
    if not product:
        m = BRACKET_RE.search(title)
        if m:
            product = TRAILING_KIND_RE.sub("", m.group(1)).strip()

    vendor = fields.get("제조사") or fields.get("거래처")

    qty, unit = None, None
    qty_raw = fields.get("발주수량")
    if qty_raw:
        seg = qty_raw.split("/")[-1].strip()          # 단위가 두 개면 뒤쪽 값 우선
        m = re.search(r"([\d,]+)\s*([^\d,\s]+)?", seg)
        if m:
            try:
                qty = int(m.group(1).replace(",", ""))
            except ValueError:
                qty = None
            unit = m.group(2) or None

    due = fields.get("납기일") or fields.get("예상납기일") or fields.get("목표납기일")

    dt = datetime.fromtimestamp(float(ts), KST)
    date = dt.strftime("%Y-%m-%d")

    reasons = []
    if not product:
        reasons.append("제품명 인식 실패")
    if qty is None and not due:
        reasons.append("발주수량·납기일 모두 인식 실패")

    return {
        "ts": ts,
        "date": date,
        "author": slack_user_name(token, msg.get("user")),
        "kind": kind,
        "product": product or f"(제품명 미확인) {title[:40]}",
        "vendor": vendor,
        "qty": qty,
        "unit": unit,
        "due": due,
        "slackUrl": f"{channel_url}/p{ts.replace('.', '')}" if channel_url else None,
        "channel": "slack", "isOverseas": False, "origin": "domestic",
        "source": "realtime-slack",
        "needsReview": bool(reasons),
        "reviewReason": " · ".join(reasons) if reasons else None,
    }


def fetch_slack_orders(token, channel_id, channel_url, oldest_ts=None, limit=200):
    orders, cursor = [], None
    while True:
        params = {"channel": channel_id, "limit": min(limit, 200)}
        if oldest_ts:
            params["oldest"] = oldest_ts
        if cursor:
            params["cursor"] = cursor
        body = slack_call("conversations.history", token, params)
        for msg in body.get("messages", []):
            if msg.get("subtype"):        # 채널 입장/알림 등 시스템 메시지는 건너뜀
                continue
            parsed = parse_slack_message(msg, channel_url, token)
            if parsed:
                orders.append(parsed)
        cursor = (body.get("response_metadata") or {}).get("next_cursor")
        if not cursor or len(orders) >= limit:
            break
    return orders


# ══════════════════════════════════════════════════════════════
#  병합 — ts(슬랙 메시지 고유 시각) 로 중복 제거
# ══════════════════════════════════════════════════════════════
def merge_orders(existing, new_entries):
    known_ts = {o.get("ts") for o in existing}
    added = []
    for o in new_entries:
        if not o.get("ts") or o["ts"] in known_ts:
            continue
        existing.append(o)
        known_ts.add(o["ts"])
        added.append(o)
    return added


# ══════════════════════════════════════════════════════════════
#  빌드 + git commit/push
# ══════════════════════════════════════════════════════════════
def run_build():
    r = subprocess.run([sys.executable, str(BUILD / "build.py")], cwd=ROOT,
                        capture_output=True, text=True)
    if r.stdout:
        sys.stdout.write(r.stdout)
    if r.stderr:
        sys.stderr.write(r.stderr)
    return r.returncode == 0


def _git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def git_commit_and_push(paths, message, max_retries=3):
    _git("add", *paths)
    if _git("diff", "--cached", "--quiet").returncode == 0:
        log("git — 변경 사항 없음, 커밋 생략")
        return True

    author = os.environ.get("GIT_AUTHOR_NAME", "pb-realtime-sync")
    author_email = os.environ.get("GIT_AUTHOR_EMAIL", "pb-realtime-sync@users.noreply.github.com")
    os.environ.update(GIT_AUTHOR_NAME=author, GIT_AUTHOR_EMAIL=author_email,
                       GIT_COMMITTER_NAME=author, GIT_COMMITTER_EMAIL=author_email)

    commit = _git("commit", "-m", message)
    if commit.returncode != 0:
        log(f"git commit 실패 — {commit.stderr.strip()}")
        return False

    # 토큰을 원격 URL 에 직접 박지 않고 extraheader 로 넘긴다 —
    # 실패 메시지가 원격 URL 을 그대로 echo 하는 경우가 많아, URL 에 토큰을
    # 넣으면 로그에 노출될 수 있다.
    extra = []
    token = os.environ.get("GITHUB_PUSH_TOKEN")
    if token:
        b64 = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        extra = ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {b64}"]

    for attempt in range(1, max_retries + 1):
        res = _git(*extra, "push", "origin", "HEAD:main")
        if res.returncode == 0:
            log("git push 완료")
            return True
        log(f"git push 실패 (시도 {attempt}/{max_retries}) — {res.stderr.strip()[-300:]}")
        _git("fetch", "origin", "main")
        if _git("rebase", "origin/main").returncode != 0:
            _git("rebase", "--abort")
            log("git rebase 충돌 — 자동 해결 불가, 사람이 확인해야 합니다")
            return False
    log("git push 3회 실패 — 다음 주기에 재시도합니다")
    return False


# ══════════════════════════════════════════════════════════════
#  실행 1회
# ══════════════════════════════════════════════════════════════
def run_once(dry_run=False, no_git=False, lookback_min=180):
    load_dotenv()
    cfg = load_json(BUILD / "config.json", {}) or {}
    slack_cfg = cfg.get("slack") or {}

    slack_token = os.environ.get("SLACK_BOT_TOKEN")

    new_entries = []

    if slack_token and slack_cfg.get("channelId"):
        oldest = str(time.time() - lookback_min * 60)
        try:
            got = fetch_slack_orders(slack_token, slack_cfg["channelId"],
                                      slack_cfg.get("url", ""), oldest_ts=oldest)
            log(f"슬랙 — 최근 {lookback_min:.0f}분 메시지 {len(got)}건 파싱")
            new_entries += got
        except Exception as e:                              # noqa: BLE001
            log(f"슬랙 수집 실패 — {e}")
    else:
        log("SLACK_BOT_TOKEN 또는 config.json 의 slack.channelId 가 없어 슬랙 수집을 건너뜁니다")

    if not new_entries:
        log("신규 항목 없음")
        return {"added": 0}

    orders = load_json(BUILD / "orders.json", [])
    added = merge_orders(orders, new_entries)
    if not added:
        log("중복 제외 신규 0건")
        return {"added": 0}

    n_review = sum(1 for a in added if a.get("needsReview"))
    log(f"신규 발주 {len(added)}건 추가 (확인필요 {n_review}건)")
    for a in added:
        flag = f" ⚠ 확인필요: {a['reviewReason']}" if a.get("needsReview") else ""
        log(f"  + [{a['source']}] {a['product']} / {a.get('qty')} {a.get('unit') or ''} "
            f"/ 납기 {a.get('due')}{flag}")

    if dry_run:
        log("[dry-run] orders.json 을 쓰지 않았습니다")
        return {"added": len(added), "dry_run": True}

    save_json(BUILD / "orders.json", orders)

    if not run_build():
        log("build.py 실패 — orders.json 은 갱신됐지만 index.html 반영·git push 는 건너뜁니다")
        return {"added": len(added), "build_ok": False}

    if no_git:
        log("--no-git — git commit/push 건너뜀")
        return {"added": len(added), "build_ok": True}

    ok = git_commit_and_push(
        ["build/orders.json", "build/data.json", "index.html"],
        f"실시간 발주 자동 수집 (+{len(added)}건, 확인필요 {n_review}건)",
    )
    return {"added": len(added), "build_ok": True, "pushed": ok}


def start_background_loop(interval_min=2.0, lookback_min=180.0):
    """dashboard.py 에서 REALTIME_SYNC=1 일 때 호출 — 데몬 스레드로 주기 실행."""
    def _loop():
        log(f"[realtime-sync] 백그라운드 수집 시작 — {interval_min:.1f}분 간격")
        while True:
            try:
                run_once(lookback_min=lookback_min)
            except Exception as e:                          # noqa: BLE001
                log(f"[realtime-sync] 주기 실행 중 오류 — {e}")
            time.sleep(interval_min * 60)

    t = threading.Thread(target=_loop, daemon=True, name="realtime-sync")
    t.start()
    return t


def main():
    ap = argparse.ArgumentParser(description="슬랙 발주 실시간 수집")
    ap.add_argument("--loop", action="store_true", help="주기적으로 반복 실행 (기본은 1회)")
    ap.add_argument("--interval-min", type=float, default=2.0, help="--loop 반복 주기(분)")
    ap.add_argument("--lookback-min", type=float, default=180.0, help="슬랙 조회 시작 시각(분 전)")
    ap.add_argument("--dry-run", action="store_true", help="조회·파싱만 하고 저장/빌드/커밋 생략")
    ap.add_argument("--no-git", action="store_true", help="저장·빌드는 하되 git commit/push 생략")
    args = ap.parse_args()

    if args.loop:
        log(f"실시간 수집 루프 시작 — {args.interval_min}분 간격")
        while True:
            try:
                run_once(dry_run=args.dry_run, no_git=args.no_git, lookback_min=args.lookback_min)
            except Exception as e:                          # noqa: BLE001
                log(f"주기 실행 중 오류 — {e}")
            time.sleep(args.interval_min * 60)
    else:
        run_once(dry_run=args.dry_run, no_git=args.no_git, lookback_min=args.lookback_min)


if __name__ == "__main__":
    main()
