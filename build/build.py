#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
파마브로스 제품 개발 대시보드 빌드 스크립트
=============================================
build/template.html + build/sheet.json + build/orders.json + build/notion.json
        → index.html  (+ build/data.json)

실행 (저장소 루트에서):
    python3 build/build.py --notion build/notion.json

안전장치가 걸리면 "중단:" 으로 시작하는 메시지를 출력하고 종료코드 1 로 끝납니다.
이때 index.html 은 손대지 않습니다.  자세한 설명은 build/README.md 참고.
"""
import argparse, json, re, sys, unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

BUILD = Path(__file__).resolve().parent
ROOT = BUILD.parent
KST = timezone(timedelta(hours=9))

# ── 안전장치 임계값 ───────────────────────────────────────────
MIN_TOTAL_RATIO = 0.80   # 총 항목이 직전 대비 이 비율 미만이면 중단
MIN_NOTION_RATIO = 0.50  # 노션 항목이 직전 대비 이 비율 미만이면 중단
NOTION_GUARD_FLOOR = 4   # 직전 노션 항목이 이 수 미만이면 노션 급감 검사 생략
MATCH_MIN_LEN = 4        # 노션↔시트 이름 매칭 시 시트 이름 최소 정규화 길이


def die(msg):
    print(f"중단: {msg}")
    sys.exit(1)


def load_json(path, what):
    p = Path(path)
    if not p.exists():
        die(f"{what} 파일이 없습니다 — {p}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        die(f"{what} 파일이 올바른 JSON 이 아닙니다 — {p} ({e})")


def norm(x):
    """이름 비교용 정규화: 유니코드 정규화 → 소문자 → 영숫자·한글만 남김"""
    if not x:
        return ""
    return re.sub(r"[^0-9a-z가-힣]", "", unicodedata.normalize("NFKC", str(x)).lower())


def datepart(v):
    """'2026-07-29 06:16:12Z' / '2026-07-29T...' → '2026-07-29'"""
    if not v:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})", str(v))
    return m.group(1) if m else None


def jlist(v):
    """노션 multi_select / person 컬럼은 JSON 문자열로 넘어온다"""
    if v is None or v == "":
        return []
    if isinstance(v, list):
        return v
    try:
        out = json.loads(v)
        return out if isinstance(out, list) else [out]
    except (json.JSONDecodeError, TypeError):
        return [v]


def page_id(url):
    """노션 url → 32자리 hex 페이지 id"""
    if not url:
        return None
    tail = str(url).rstrip("/").split("/")[-1].split("?")[0]
    hexes = re.findall(r"[0-9a-f]{32}", tail.replace("-", ""))
    return hexes[0] if hexes else (tail or None)


# ══════════════════════════════════════════════════════════════
#  노션 행 → 대시보드 항목
# ══════════════════════════════════════════════════════════════
def notion_to_item(row, owners_map, carry, warn):
    pid = page_id(row.get("url"))
    if not pid:
        return None

    raw2 = row.get("Status 2") or None
    s1 = row.get("Status 1") or None
    if s1 == "중단":
        stage = status2 = "중단"
        note = f"중단 시점 단계: {raw2}" if raw2 else None
    else:
        stage = status2 = raw2
        note = None

    brands = jlist(row.get("브랜드"))
    makers = jlist(row.get("제조사"))

    owners = []
    for uid in jlist(row.get("담당자")):
        uid = str(uid).replace("user://", "").strip()
        if uid in owners_map:
            owners.append(owners_map[uid])
        else:
            warn.append(f"모르는 담당자 ID: {uid} (페이지: {row.get('이름')})")

    kept = carry.get(pid, {})

    return {
        "key": "N" + pid,
        "src": "notion",
        "no": None,
        "code": None,
        "name": row.get("이름") or "(이름 없음)",
        "brand": brands[0] if brands else "미정",
        "ptype": row.get("유형") or None,
        "pack": None, "unitWeight": None, "spec": None, "dose": None,
        "ingr": None, "func": None, "shelf": None,
        "maker": makers[0] if makers else None,
        "makers": makers,
        "market": "국내",
        "note": note,
        "stage": stage,
        "status1": s1,
        "status2": status2,
        "origStatus2": raw2,
        "classes": [],
        "owners": owners,
        "pharmacist": None,
        "drive": None,
        "start": kept.get("start"),
        "target": datepart(row.get("date:입고 목표일:start")),
        "nextCheck": datepart(row.get("date:다음 확인 예정일:start")),
        "end": None,
        "notionUrl": f"https://app.notion.com/p/{pid}",
        "edited": datepart(row.get("최근 편집일")),
        "created": kept.get("created") or datepart(row.get("createdTime")),
        "po": None,
        "isOverseas": False,
        "origin": "domestic",
    }


# ══════════════════════════════════════════════════════════════
#  발주 → 항목 po 집계
# ══════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════
#  항목 스키마 정규화
#  UI 가 기대하는 필드 집합·순서를 항목마다 동일하게 맞춘다.
#  (없는 값은 null, 목록형은 빈 배열 — 원본 index.html 과 동작 동일)
# ══════════════════════════════════════════════════════════════
FIELDS = ["key", "src", "no", "code", "name", "devName", "brand", "ptype",
          "pack", "unitWeight", "spec", "dose", "ingr", "func", "shelf",
          "maker", "makers", "note", "market", "stage", "status1", "status2",
          "origStatus2", "owners", "pharmacist", "classes", "drive",
          "start", "target", "nextCheck", "end",
          "notionUrl", "edited", "created", "po", "isOverseas", "origin"]
LIST_FIELDS = {"owners", "classes", "makers"}


def normalize_item(it):
    out = {}
    for f in FIELDS:
        v = it.get(f)
        if f in LIST_FIELDS and not isinstance(v, list):
            v = [] if v in (None, "") else [v]
        out[f] = v
    extra = [f for f in it if f not in FIELDS]
    for f in extra:
        out[f] = it[f]
    return out


def attach_po(items, orders):
    by_key = {}
    for o in orders:
        k = o.get("matchKey")
        if k:
            by_key.setdefault(k, []).append(o)
    for it in items:
        rel = by_key.get(it["key"])
        if not rel:
            it["po"] = None
            continue
        rel = sorted(rel, key=lambda o: o.get("date") or "")
        it["po"] = {
            "count": len(rel),
            "qty": sum(o.get("qty") or 0 for o in rel),
            "last": rel[-1].get("date"),
            "unit": rel[-1].get("unit"),
        }


def fill_missing_order_matches(orders, sheet, warn):
    """match 판단이 아예 없는 발주만 보수적으로 자동 매칭 (기존 판단은 절대 건드리지 않음)"""
    cand = [(norm(r["name"]), r) for r in sheet if len(norm(r["name"])) >= MATCH_MIN_LEN]
    for o in orders:
        if "match" in o:
            continue
        p = norm(o.get("product"))
        hits = sorted([r for n, r in cand if n and n in p],
                      key=lambda r: -len(norm(r["name"])))
        if len(hits) == 1 or (hits and len(norm(hits[0]["name"])) > len(norm(hits[1]["name"]))):
            r = hits[0]
            v = norm(o.get("vendor"))
            o.update(match="full", matchKey=r["key"], matchName=r["name"],
                     makerOk=any(v and (v in norm(m) or norm(m) in v)
                                 for m in (r.get("makers") or [])))
            warn.append(f"발주 자동 매칭: {o.get('product')} → {r['key']} {r['name']}")
        else:
            o.update(match="none", matchKey=None, matchName=None, makerOk=None)
            warn.append(f"발주 매칭 실패(수동 확인 필요): {o.get('product')}")


# ══════════════════════════════════════════════════════════════
#  직전 대비 변경 리포트
# ══════════════════════════════════════════════════════════════
WATCH = [("name", "이름"), ("status1", "Status 1"), ("status2", "Status 2"),
         ("stage", "단계"), ("brand", "브랜드"), ("maker", "제조사"),
         ("ptype", "유형"), ("target", "입고 목표일"), ("nextCheck", "다음 확인 예정일")]


def fmt(v):
    if v is None or v == "" or v == []:
        return "-"
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return str(v)


def report(prev_items, items):
    prev = {x["key"]: x for x in (prev_items or [])}
    cur = {x["key"]: x for x in items}
    added = [cur[k] for k in cur if k not in prev]
    removed = [prev[k] for k in prev if k not in cur]

    changes = []
    for k in cur:
        if k not in prev:
            continue
        a, b = prev[k], cur[k]
        diffs = [(lab, fmt(a.get(f)), fmt(b.get(f)))
                 for f, lab in WATCH if fmt(a.get(f)) != fmt(b.get(f))]
        ow_a, ow_b = fmt(a.get("owners")), fmt(b.get("owners"))
        if ow_a != ow_b:
            diffs.append(("담당자", ow_a, ow_b))
        if diffs:
            changes.append((b, diffs))

    lines = ["■ 직전 대비 변경"]
    if not (added or removed or changes):
        lines.append("  변경 없음")
        return lines, False

    def tag(x):
        return "노션" if x.get("src") == "notion" else ("시트+노션" if x.get("src") == "both" else "시트")

    if added:
        lines.append(f"  · 신규 {len(added)}건")
        for x in added:
            lines.append(f"      + [{tag(x)}] {x.get('name')} — "
                         f"{fmt(x.get('status1'))} / {fmt(x.get('status2'))} "
                         f"(담당 {fmt(x.get('owners'))})")
    if removed:
        lines.append(f"  · 삭제 {len(removed)}건")
        for x in removed:
            lines.append(f"      - [{tag(x)}] {x.get('name')}")
    if changes:
        lines.append(f"  · 변경 {len(changes)}건")
        for x, diffs in changes:
            lines.append(f"      ~ [{tag(x)}] {x.get('name')}")
            for lab, a, b in diffs:
                lines.append(f"          {lab}: {a} → {b}")
    return lines, True


# ══════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--notion", default=str(BUILD / "notion.json"),
                    help="노션 query-data-sources 결과 (results 배열)")
    ap.add_argument("--allow-empty", action="store_true",
                    help="안전장치 무시 (정상 운영에서는 절대 쓰지 마세요)")
    ap.add_argument("--out", default=str(ROOT / "index.html"))
    args = ap.parse_args()

    cfg = load_json(BUILD / "config.json", "설정(config.json)")
    sheet = load_json(BUILD / "sheet.json", "시트 스냅샷(sheet.json)")
    orders = load_json(BUILD / "orders.json", "발주 스냅샷(orders.json)")
    carry = load_json(BUILD / "carry.json", "보존값(carry.json)")
    owners_map = load_json(BUILD / "owners.json", "담당자 매핑(owners.json)")
    tpl_path = BUILD / "template.html"
    if not tpl_path.exists():
        die(f"템플릿이 없습니다 — {tpl_path}")
    tpl = tpl_path.read_text(encoding="utf-8")
    if tpl.count("__DB_JSON__") != 1:
        die(f"템플릿의 __DB_JSON__ 자리표시자가 1개가 아닙니다 "
            f"({tpl.count('__DB_JSON__')}개) — template.html 이 손상됐습니다")

    raw = load_json(args.notion, "노션 결과(notion.json)")
    if isinstance(raw, dict):
        raw = raw.get("results", raw.get("items", []))
    if not isinstance(raw, list):
        die("노션 결과가 배열이 아닙니다 — results 배열을 그대로 저장했는지 확인하세요")

    # ── 안전장치 1: 노션 0건 ──
    if len(raw) == 0 and not args.allow_empty:
        die("노션 조회 결과가 0건입니다 — 조회 실패이거나 권한 문제일 수 있습니다. "
            "index.html 을 덮어쓰지 않았습니다")

    warn = []
    notion_items = [x for x in (notion_to_item(r, owners_map, carry, warn) for r in raw) if x]

    # ── 노션 ↔ 시트 매칭 (정규화 후 완전일치 또는 노션이름 ⊇ 시트이름, 유일할 때만) ──
    cand = [(norm(r["name"]), r) for r in sheet if len(norm(r["name"])) >= MATCH_MIN_LEN]
    sheet_by_key = {r["key"]: dict(r) for r in sheet}
    matched_keys, standalone = set(), []
    for n in notion_items:
        nn = norm(n["name"])
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

    for k, r in sheet_by_key.items():
        r.setdefault("src", "sheet")

    items = [normalize_item(sheet_by_key[r["key"]]) for r in sheet] + \
            [normalize_item(x) for x in sorted(standalone, key=lambda x: x["key"])]

    fill_missing_order_matches(orders, sheet, warn)
    attach_po(items, orders)

    n_sheet = sum(1 for x in items if x["src"] in ("sheet", "both"))
    n_notion = sum(1 for x in items if x["src"] in ("notion", "both"))

    # ── 안전장치 2: 직전 대비 급감 ──
    prev = None
    prev_path = BUILD / "data.json"
    if prev_path.exists():
        try:
            prev = json.loads(prev_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            prev = None
    prev_items = (prev or {}).get("items") or []

    if prev_items and not args.allow_empty:
        pt, ct = len(prev_items), len(items)
        if ct < pt * MIN_TOTAL_RATIO:
            die(f"총 항목이 급감했습니다 ({pt}건 → {ct}건, "
                f"{MIN_TOTAL_RATIO:.0%} 미만) — index.html 을 덮어쓰지 않았습니다")
        pn = sum(1 for x in prev_items if x.get("src") in ("notion", "both"))
        if pn >= NOTION_GUARD_FLOOR and n_notion < pn * MIN_NOTION_RATIO:
            die(f"노션 항목이 급감했습니다 ({pn}건 → {n_notion}건, "
                f"{MIN_NOTION_RATIO:.0%} 미만) — index.html 을 덮어쓰지 않았습니다")

    # ── DB 조립 ──
    db = {
        "sheetTitle": cfg.get("sheetTitle"), "sheetUrl": cfg.get("sheetUrl"),
        "notionTitle": cfg.get("notionTitle"), "notionUrl": cfg.get("notionUrl"),
        "process": cfg.get("process") or [],
        "synced_at": datetime.now(KST).isoformat(timespec="seconds"),
        "items": items, "orders": orders,
        "slack": cfg.get("slack") or {}, "gmail": cfg.get("gmail") or {},
    }

    blob = json.dumps(db, ensure_ascii=False, separators=(",", ":")) \
               .replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    html = tpl.replace("__DB_JSON__", blob)

    Path(args.out).write_text(html, encoding="utf-8")
    prev_path.write_text(json.dumps(db, ensure_ascii=False, indent=1), encoding="utf-8")

    # ── 출력 ──
    lines, changed = report(prev_items, items)
    print("\n".join(lines))
    print()
    print(f"■ 집계")
    print(f"  · 총 항목 {len(items)}건  (시트 {n_sheet} / 노션 {n_notion})")
    print(f"  · 발주 {len(orders)}건")
    print(f"  · 동기화 시각 {db['synced_at']}")
    print(f"  · 출력 {args.out} ({len(html.encode('utf-8')):,} bytes)")
    if warn:
        print()
        print("■ 경고")
        for w in dict.fromkeys(warn):
            print(f"  ! {w}")
    print()
    print("빌드 완료" + ("" if changed else " (내용 변경 없음)"))


if __name__ == "__main__":
    main()
