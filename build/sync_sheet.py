#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
구글시트 "제품 스펙 정리표" 동기화
==================================
구글시트 CSV 를 build/sheet.json 에 병합합니다. **스펙(사양) 필드만** 갱신하고,
판매상태(stage/status1/status2/market/isOverseas/origin 등)는 절대 건드리지 않습니다.
그 값들은 이 시트에 없는 정보이기 때문입니다 (build/README.md 참고).

사용:
    python3 build/sync_sheet.py build/sheet_raw.csv

동작:
  · 품번(코드)으로 기존 항목과 매칭 → 있으면 스펙 필드만 갱신
  · 품번이 없으면 브랜드+제품명 정규화로 매칭
  · 매칭 안 되면 새 항목 추가 (판매상태는 "확인 필요" 로 표시, 경고 출력)
  · 기존 항목이 새 CSV 에 없으면 삭제하지 않고 경고만 출력
"""
import csv, json, re, sys, unicodedata
from pathlib import Path

BUILD = Path(__file__).resolve().parent

HEADER_MAP = {
    "번호": "no", "브랜드": "brand", "품번": "code", "제품명": "name",
    "제품유형": "ptype", "포장단위": "pack",
    "단위중량 (1정/포당)": "unitWeight", "포장단위 및 중량": "spec",
    "섭취량 및 섭취방법": "dose", "기능성성분 및 함량": "ingr",
    "기능성 내용": "func", "소비기한": "shelf", "제조원": "maker", "비고": "note",
}
SPEC_FIELDS = ["no", "brand", "code", "name", "ptype", "pack", "unitWeight",
               "spec", "dose", "ingr", "func", "shelf", "maker", "note"]
# 시트에 없는(=건드리면 안 되는) 판매상태·분류 필드
PROTECTED = ["stage", "status1", "status2", "market", "start", "target",
             "nextCheck", "end", "drive", "pharmacist", "classes",
             "isOverseas", "origin"]


def norm(x):
    if not x:
        return ""
    return re.sub(r"[^0-9a-z가-힣]", "", unicodedata.normalize("NFKC", str(x)).lower())


def parse_num(x):
    if x is None or x == "":
        return None
    try:
        return int(str(x).replace(",", "").strip())
    except ValueError:
        return x


def main():
    if len(sys.argv) != 2:
        print("사용법: python3 build/sync_sheet.py <csv 파일>")
        sys.exit(1)
    csv_path = Path(sys.argv[1])
    if not csv_path.exists():
        print(f"중단: CSV 파일이 없습니다 — {csv_path}")
        sys.exit(1)

    sheet_path = BUILD / "sheet.json"
    sheet = json.loads(sheet_path.read_text(encoding="utf-8")) if sheet_path.exists() else []
    by_code = {r["code"]: r for r in sheet if r.get("code")}
    by_name = {norm(f"{r.get('brand')}{r.get('name')}"): r for r in sheet}

    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    header = None
    for r in rows:
        if any(h in r for h in ("번호", "제품명")):
            header = r
            data_rows = rows[rows.index(r) + 1:]
            break
    if header is None:
        print("중단: CSV 에서 헤더 행을 찾지 못했습니다 (번호/제품명 컬럼 필요)")
        sys.exit(1)

    idx = {}
    for i, h in enumerate(header):
        h = h.strip()
        if h in HEADER_MAP:
            idx[HEADER_MAP[h]] = i
    missing_headers = [k for k in HEADER_MAP.values() if k not in idx]

    seen_codes, seen_names = set(), set()
    added, changed, warnings = [], [], []
    max_no = max([r.get("no") or 0 for r in sheet], default=0)

    for row in data_rows:
        if not any(c.strip() for c in row):
            continue
        get = lambda f: (row[idx[f]].strip() if f in idx and idx[f] < len(row) else None) or None
        name = get("name")
        if not name:
            continue
        code = get("code")
        rec = {f: get(f) for f in SPEC_FIELDS}
        rec["no"] = parse_num(rec["no"])
        if not rec["no"]:
            rec["no"] = None

        target = by_code.get(code) if code else None
        if not target:
            target = by_name.get(norm(f"{rec.get('brand')}{name}"))

        if target:
            if code:
                seen_codes.add(code)
            seen_names.add(norm(f"{rec.get('brand')}{name}"))
            diffs = [(f, target.get(f), rec.get(f)) for f in SPEC_FIELDS
                     if rec.get(f) is not None and target.get(f) != rec.get(f)]
            if diffs:
                for f, old, new in diffs:
                    target[f] = new
                target["makers"] = [rec["maker"]] if rec.get("maker") else target.get("makers", [])
                changed.append((target["key"], name, diffs))
        else:
            max_no += 1
            new_key = f"S{max([int(r['key'][1:]) for r in sheet if r['key'][0]=='S'] + [0]) + 1}"
            new_item = dict(rec)
            new_item.update(key=new_key, src="sheet", devName=None,
                            market=None, stage=None, status1="확인 필요", status2=None,
                            origStatus2=None, owners=[], start=None, target=None,
                            nextCheck=None, end=None, notionUrl=None, edited=None,
                            created=None, drive=None, pharmacist=None, classes=[],
                            makers=[rec["maker"]] if rec.get("maker") else [],
                            po=None, isOverseas=False, origin="domestic")
            sheet.append(new_item)
            by_code[code] = new_item if code else None
            by_name[norm(f"{rec.get('brand')}{name}")] = new_item
            added.append(new_item)
            warnings.append(f"신규 시트 항목 추가 — 판매 상태 확인 필요: {name} ({new_key})")
            if code:
                seen_codes.add(code)
            seen_names.add(norm(f"{rec.get('brand')}{name}"))

    for r in sheet:
        key_seen = (r.get("code") in seen_codes) if r.get("code") else \
                   (norm(f"{r.get('brand')}{r.get('name')}") in seen_names)
        if not key_seen:
            warnings.append(f"시트에서 사라짐(항목은 유지함, 확인 필요): {r.get('name')} ({r['key']})")

    sheet_path.write_text(json.dumps(sheet, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"■ 시트 동기화 결과 (스펙 필드만 갱신, 판매상태는 유지)")
    print(f"  · 신규 {len(added)}건, 변경 {len(changed)}건, 총 {len(sheet)}건")
    for k, name, diffs in changed:
        print(f"    ~ [{k}] {name}: " + ", ".join(f"{f} {old!r}→{new!r}" for f, old, new in diffs))
    if missing_headers:
        warnings.append(f"CSV 에 없는 예상 컬럼: {missing_headers} (해당 필드는 갱신되지 않음)")
    if warnings:
        print("\n■ 경고")
        for w in warnings:
            print(f"  ! {w}")


if __name__ == "__main__":
    main()
