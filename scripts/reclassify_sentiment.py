#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
이미 대시보드 데이터(docs/data/YYYY-MM.json)에 저장된 기사를 현재 분류기(esg_classifier)의
판정 기준으로 다시 판별해 감성 라벨을 바로잡는다.

기본 대상은 '저장된 모든 기사'(POSITIVE / NEGATIVE / UNCERTAIN) 이며,
현재 기준(CLF_VERSION)으로 아직 재분류하지 않은 기사만 처리한다. 그래서 호출 상한에 걸려 멈춰도
다시 실행하면 이어서 진행된다. (기준을 또 바꾸면 CLF_VERSION 을 올리면 전부 다시 대상이 된다)

  python scripts/reclassify_sentiment.py --dry-run --limit 20      # 20건만 미리보기(파일 수정 안 함)
  python scripts/reclassify_sentiment.py                           # 전체 재분류(우선순위: 보류 -> 부정 -> 긍정)
  python scripts/reclassify_sentiment.py --labels UNCERTAIN,NEGATIVE   # 일부 라벨만
  python scripts/reclassify_sentiment.py --force                   # 이미 재분류한 기사도 다시
  python scripts/reclassify_sentiment.py --drop-excluded           # ESG 무관/중립으로 판정된 건은 삭제

동작 규칙
  - 새 결과가 기존과 다르면 sentiment 를 바꾸고, 이전 값은 prev_sentiment 에 보존
  - 새 결과 제외(비ESG/중립)  -> 기본은 라벨 유지 + 보고서에 '제외후보'로 표시, --drop-excluded 시 삭제
  - 호출 실패/응답 누락       -> 그대로 두고 다음 실행에서 재시도
  - 배치마다 파일을 저장하므로 중간에 멈춰도 진행분이 보존됨
이메일은 발송하지 않는다. Gemini 호출 상한: --max-calls (기본 150, 배치당 기사 6건).
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

# 분류 기준(프롬프트/판정 정책)을 바꿀 때마다 올린다.
#  1: 부정 근거 검증 도입   2: 소비자 관점(부정 요소 없으면 긍정, 혼재만 보류)
CLF_VERSION = 2

ALL_LABELS = ["UNCERTAIN", "NEGATIVE", "POSITIVE"]  # 처리 우선순위(보류·부정을 먼저)


def save_files(data, dropped_ids):
    for fp, arr in data.items():
        kept = [r for r in arr if id(r) not in dropped_ids]
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(kept, f, ensure_ascii=False, indent=2)
        data[fp] = kept


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="docs/data")
    ap.add_argument("--batch-size", type=int, default=6)
    ap.add_argument("--max-calls", type=int, default=150)
    ap.add_argument("--limit", type=int, default=0, help="처리할 최대 기사 수(0=전부)")
    ap.add_argument("--labels", default=",".join(ALL_LABELS),
                    help="재분류할 기존 라벨(쉼표 구분). 기본: UNCERTAIN,NEGATIVE,POSITIVE 전체")
    ap.add_argument("--include-uncertain", action="store_true",
                    help="(호환용) 기본값이 전체이므로 효과 없음")
    ap.add_argument("--force", action="store_true", help="현재 기준으로 이미 재분류한 기사도 다시 처리")
    ap.add_argument("--drop-excluded", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", default="reclassify_report.csv")
    args = ap.parse_args()

    labels = [x.strip().upper() for x in args.labels.split(",") if x.strip()]
    rank = {lb: i for i, lb in enumerate(ALL_LABELS)}
    files = sorted(glob.glob(os.path.join(args.data_dir, "20??-??.json")))
    data = {}
    todo = []
    total = done = 0
    for fp in files:
        with open(fp, encoding="utf-8") as f:
            arr = json.load(f)
        data[fp] = arr
        for rec in arr:
            total += 1
            if rec.get("sentiment") not in labels:
                continue
            if not args.force and rec.get("clf_version") == CLF_VERSION:
                done += 1
                continue
            todo.append((fp, rec))
    # 보류 -> 부정 -> 긍정 순, 같은 라벨 안에서는 최신 기사 먼저
    todo.sort(key=lambda fr: fr[1].get("date", ""), reverse=True)
    todo.sort(key=lambda fr: rank.get(fr[1].get("sentiment"), 9))
    remaining_total = len(todo)
    if args.limit:
        todo = todo[:args.limit]
    print("전체 기사 %d건 | 현재 기준(v%d)으로 이미 재분류됨 %d건 | 이번 대상 %d건 (라벨 %s)" % (
        total, CLF_VERSION, done, remaining_total, "/".join(labels)))
    if not todo:
        print("재분류할 기사가 없습니다. (RECLASSIFY_DONE)")
        return
    print("이번 실행 처리 예정 %d건, 필요 호출 약 %d회 (상한 %d회)" % (
        len(todo), -(-len(todo) // args.batch_size), args.max_calls))

    call = clf.make_gemini_caller()
    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=6)
    rows, calls, changed, dropped, fails, processed = [], 0, 0, 0, 0, 0
    drop_ids = set()
    transitions = {}

    for i in range(0, len(todo), args.batch_size):
        if calls >= args.max_calls:
            print("[중단] 호출 상한 도달 - 다시 실행하면 이어서 진행됩니다.")
            break
        chunk = todo[i:i + args.batch_size]
        pages = list(pool.map(lambda fr: clf.fetch_article(fr[1]["link"]), chunk))
        items = []
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
                continue  # 응답 누락 -> 다음 실행에서 재시도
            new, info = outcome
            old = rec.get("sentiment")
            action = "유지"
            processed += 1
            if new is None:
                action = "삭제" if args.drop_excluded else "제외후보(수동확인)"
                if args.drop_excluded and not args.dry_run:
                    drop_ids.add(id(rec))
                    dropped += 1
            elif new != old:
                action = "%s->%s" % (old, new)
                changed += 1
                transitions[action] = transitions.get(action, 0) + 1
                if not args.dry_run:
                    rec["prev_sentiment"] = old
                    rec["sentiment"] = new
            if not args.dry_run:
                rec["event_type"] = info.get("event_type", "")
                rec["reclassified"] = True
                rec["clf_version"] = CLF_VERSION
            rows.append([rec.get("date"), rec.get("company"), old, new or "EXCLUDED", action,
                         info.get("event_type", ""), info.get("reason", ""), info.get("note", ""),
                         rec.get("title"), rec.get("link")])
            print("  %-18s %s | %s" % (action, (rec.get("company") or "")[:8], (rec.get("title") or "")[:40]))
        if not args.dry_run:
            save_files(data, drop_ids)  # 배치마다 저장
        print("  [진행] 호출 %d/%d회, 처리 %d건, 라벨 변경 %d건" % (calls, args.max_calls, processed, changed))

    with open(args.report, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "company", "old", "new", "action", "event_type", "reason", "note", "title", "link"])
        w.writerows(rows)
    print("\n완료: 처리 %d건 | 라벨 변경 %d건 | 삭제 %d건 | Gemini 호출 %d회%s" % (
        processed, changed, dropped, calls, " (dry-run: 파일 미수정)" if args.dry_run else ""))
    if transitions:
        print("변경 내역: " + ", ".join("%s %d건" % (k, v) for k, v in sorted(transitions.items())))
    left = max(0, remaining_total - processed)
    print("남은 대상: 약 %d건 -> %s. 보고서: %s" % (
        left, "다시 실행하면 이어서 진행" if left and not args.dry_run else "없음/미리보기", args.report))


if __name__ == "__main__":
    main()
