#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
이미 대시보드 데이터(docs/data/YYYY-MM.json)에 저장된 기사 중 NEGATIVE(필요 시 UNCERTAIN) 건을
개선된 분류기(esg_classifier)로 다시 판별해 감성 라벨을 바로잡는다.

  python scripts/reclassify_sentiment.py --dry-run --limit 20     # 20건만 미리보기(파일 수정 안 함)
  python scripts/reclassify_sentiment.py                          # NEGATIVE 전체 재분류
  python scripts/reclassify_sentiment.py --include-uncertain
  python scripts/reclassify_sentiment.py --drop-excluded          # ESG 무관/중립으로 판정된 건은 삭제

동작 규칙
  - 새 결과 POSITIVE            -> sentiment 를 POSITIVE 로 변경 (이전 값은 prev_sentiment 에 보존)
  - 새 결과 NEGATIVE(근거 확인) -> 유지
  - 새 결과 UNCERTAIN(보류)     -> UNCERTAIN 으로 변경
  - 새 결과 제외(비ESG/중립)    -> 기본은 보고서에만 표시, --drop-excluded 시 삭제
  - 호출 실패/응답 누락         -> 그대로 두고 다음 실행에서 재시도
이메일은 발송하지 않는다. Gemini 호출 상한: --max-calls (기본 150).
필요 환경변수: GEMINI_API_KEY (선택: GEMINI_MODEL, GEMINI_MIN_INTERVAL)
"""
import os
import sys
import csv
import json
import glob
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import esg_classifier as clf  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="docs/data")
    ap.add_argument("--batch-size", type=int, default=6)
    ap.add_argument("--max-calls", type=int, default=150)
    ap.add_argument("--limit", type=int, default=0, help="처리할 최대 기사 수(0=전부)")
    ap.add_argument("--include-uncertain", action="store_true")
    ap.add_argument("--drop-excluded", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", default="reclassify_report.csv")
    args = ap.parse_args()

    targets_labels = {"NEGATIVE"} | ({"UNCERTAIN"} if args.include_uncertain else set())
    files = sorted(glob.glob(os.path.join(args.data_dir, "20??-??.json")))
    data = {}
    todo = []
    for fp in files:
        with open(fp, encoding="utf-8") as f:
            arr = json.load(f)
        data[fp] = arr
        for rec in arr:
            if rec.get("sentiment") in targets_labels and not rec.get("reclassified"):
                todo.append((fp, rec))
    if args.limit:
        todo = todo[:args.limit]
    print("재분류 대상: %d건 (라벨 %s)" % (len(todo), "/".join(sorted(targets_labels))))
    if not todo:
        return

    call = clf.make_gemini_caller()
    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=6)
    rows, calls, changed, dropped, fails = [], 0, 0, 0, 0
    drop_ids = set()

    for i in range(0, len(todo), args.batch_size):
        if calls >= args.max_calls:
            print("[중단] 호출 상한 도달 - 다시 실행하면 이어서 진행됩니다.")
            break
        chunk = todo[i:i + args.batch_size]
        pages = list(pool.map(lambda fr: clf.fetch_article(fr[1]["link"]), chunk))
        items, idx = [], []
        for (fp, rec), page in zip(chunk, pages):
            if page and clf.is_paywalled(page.get("text", "")):
                page = None
            items.append({"company": rec.get("company", ""), "source_company": rec.get("source_company"),
                          "title": rec.get("title", ""),
                          "content": clf.build_content(rec.get("description", ""), page)})
        res = clf.classify_items(items, call)
        calls += 1
        if res is None:
            fails += 1
            print("  [배치 실패] 연속 %d회" % fails)
            if fails >= 2:
                print("[중단] 연속 실패 - 쿼터 소진 가능성")
                break
            continue
        fails = 0
        for (fp, rec), outcome in zip(chunk, res):
            if outcome is None:
                continue
            new, info = outcome
            old = rec.get("sentiment")
            action = "유지"
            if new is None:
                action = "삭제" if args.drop_excluded else "제외후보(수동확인)"
                if args.drop_excluded and not args.dry_run:
                    drop_ids.add(id(rec))
                    dropped += 1
            elif new != old:
                action = "%s->%s" % (old, new)
                changed += 1
                if not args.dry_run:
                    rec["prev_sentiment"] = old
                    rec["sentiment"] = new
            if not args.dry_run and new is not None:
                rec["event_type"] = info.get("event_type", "")
                rec["reclassified"] = True
            rows.append([rec.get("date"), rec.get("company"), old, new or "EXCLUDED", action,
                         info.get("event_type", ""), info.get("reason", ""), info.get("note", ""),
                         rec.get("title"), rec.get("link")])
            print("  %-18s %s | %s" % (action, (rec.get("company") or "")[:8], (rec.get("title") or "")[:40]))

    if not args.dry_run:
        for fp, arr in data.items():
            kept = [r for r in arr if id(r) not in drop_ids]
            if len(kept) != len(arr) or any(r.get("reclassified") for r in arr):
                with open(fp, "w", encoding="utf-8") as f:
                    json.dump(kept, f, ensure_ascii=False, indent=1)
    with open(args.report, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "company", "old", "new", "action", "event_type", "reason", "note", "title", "link"])
        w.writerows(rows)
    print("\n완료: 변경 %d건, 삭제 %d건, Gemini 호출 %d회%s. 보고서: %s" % (
        changed, dropped, calls, " (dry-run: 파일 미수정)" if args.dry_run else "", args.report))


if __name__ == "__main__":
    main()
