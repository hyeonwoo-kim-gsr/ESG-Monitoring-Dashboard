#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
normalize_company_names.py — docs/data/*.json 에 이미 저장된 과거 기사의 회사명을 표준명으로 통합

GS홈쇼핑 / GS SHOP / GS샵 / GSSHOP  ->  GS리테일   (원래 표기는 "source_company" 필드에 보존)

사용법 (프로젝트 루트에서)
  python scripts/normalize_company_names.py --dry-run   # 바뀔 건수만 확인
  python scripts/normalize_company_names.py             # 실제 반영
여러 번 실행해도 결과는 같습니다(멱등).
"""
import os
import re
import json
import argparse

COMPANY_ALIASES = {
    "GS홈쇼핑": "GS리테일",
    "GS SHOP": "GS리테일",
    "GS샵": "GS리테일",
    "GSSHOP": "GS리테일",
}
# 대소문자/공백 차이 흡수용 (예: "gs shop", "GS Shop")
_NORM = {re.sub(r"\s+", "", k).upper(): v for k, v in COMPANY_ALIASES.items()}


def canonical(name):
    if not isinstance(name, str):
        return name
    return COMPANY_ALIASES.get(name) or _NORM.get(re.sub(r"\s+", "", name).upper(), name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=os.environ.get("DASHBOARD_DATA_DIR", "docs/data"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    total = 0
    for fn in sorted(os.listdir(args.data_dir)):
        if not re.match(r"^\d{4}-\d{2}\.json$", fn):
            continue
        path = os.path.join(args.data_dir, fn)
        with open(path, encoding="utf-8") as f:
            recs = json.load(f)
        changed = 0
        for r in recs:
            new = canonical(r.get("company"))
            if new != r.get("company"):
                r.setdefault("source_company", r["company"])
                r["company"] = new
                changed += 1
        if changed:
            total += changed
            print("  %s: %d건 변경" % (fn, changed))
            if not args.dry_run:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(recs, f, ensure_ascii=False, indent=2)
    print("[완료] 총 %d건 %s" % (total, "(dry-run: 저장 안 함)" if args.dry_run else "변경 저장"))


if __name__ == "__main__":
    main()
