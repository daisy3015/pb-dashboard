#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
슬랙 발주 의뢰 채널 동기화
==========================
슬랙에서 새로 읽은 발주 메시지(구조화된 JSON)를 build/orders.json 에 추가합니다.

입력 JSON 은 배열이며, 각 원소는 다음 필드를 갖습니다 (build/README.md 의
"슬랙 메시지 → JSON 추출 규칙" 참고):
    ts, date, author, kind, product, vendor, qty, unit, due, slackUrl

이 스크립트는:
  · ts 가 이미 orders.json 에 있으면 건너뜁니다 (중복 방지)
  · product 가 없거나 (qty 와 due 가 모두 없음) 이면 넣지 않고 경고만 남깁니다
    (수동 확인 필요 — 발주 정보가 불완전한 메시지)
  · match/matchKey/makerOk 는 설정하지 않습니다 — build.py 가 자동 계산합니다
  · channel="slack", isOverseas=False, origin="domestic" 를 붙입니다
    (이 채널은 국내 발주 전용 — 해외 발주는 build/config.json 의 gmail 채널이며
     이 스크립트가 다루지 않습니다)

사용:
    python3 build/sync_orders.py build/slack_new.json
"""
import json, sys
from pathlib import Path

BUILD = Path(__file__).resolve().parent
REQUIRED_ANY = ("qty", "due")


def main():
    if len(sys.argv) != 2:
        print("사용법: python3 build/sync_orders.py <슬랙 추출 JSON>")
        sys.exit(1)
    src = Path(sys.argv[1])
    if not src.exists():
        print(f"중단: 입력 파일이 없습니다 — {src}")
        sys.exit(1)

    new_raw = json.loads(src.read_text(encoding="utf-8"))
    if not isinstance(new_raw, list):
        print("중단: 입력이 배열이 아닙니다")
        sys.exit(1)

    orders_path = BUILD / "orders.json"
    orders = json.loads(orders_path.read_text(encoding="utf-8")) if orders_path.exists() else []
    known_ts = {o.get("ts") for o in orders}

    added, skipped_dup, skipped_bad = [], [], []
    for raw in new_raw:
        ts = str(raw.get("ts") or "")
        if not ts:
            skipped_bad.append((raw.get("product"), "ts 없음"))
            continue
        if ts in known_ts:
            skipped_dup.append(raw.get("product"))
            continue
        if not raw.get("product") or not any(raw.get(f) for f in REQUIRED_ANY):
            skipped_bad.append((raw.get("product"), "product 또는 qty/due 누락"))
            continue
        qty = raw.get("qty")
        if isinstance(qty, str):
            try:
                qty = int(qty.replace(",", "").strip())
            except ValueError:
                pass
        order = {
            "ts": ts, "date": raw.get("date"), "author": raw.get("author"),
            "kind": raw.get("kind"), "product": raw.get("product"),
            "vendor": raw.get("vendor"), "qty": qty, "unit": raw.get("unit"),
            "due": raw.get("due"), "slackUrl": raw.get("slackUrl"),
            "channel": "slack", "isOverseas": False, "origin": "domestic",
        }
        orders.append(order)
        known_ts.add(ts)
        added.append(order)

    orders_path.write_text(json.dumps(orders, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"■ 슬랙 발주 동기화 결과")
    print(f"  · 신규 {len(added)}건 추가, 중복 스킵 {len(skipped_dup)}건, 총 {len(orders)}건")
    for o in added:
        print(f"    + {o['product']} ({o.get('qty')} {o.get('unit') or ''}, 납기 {o.get('due')})")
    if skipped_bad:
        print("\n■ 경고 — 정보 부족으로 추가하지 않음 (수동 확인 필요)")
        for product, reason in skipped_bad:
            print(f"  ! {product!r}: {reason}")


if __name__ == "__main__":
    main()
