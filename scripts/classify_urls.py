#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
URL 목록(한 줄에 하나)을 받아 본문 추출 -> 분류 결과를 화면에 출력한다 (파일/데이터 수정 없음).
잘못 분류됐던 기사로 프롬프트 개선 효과를 직접 확인하는 용도.

  $env:GEMINI_API_KEY="..."
  python scripts/classify_urls.py scripts/misclassified_urls.txt
  python scripts/classify_urls.py scripts/misclassified_urls.txt --show-text   # 모델에 들어가는 본문도 출력
"""
import os
import sys
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import esg_classifier as clf  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("urlfile")
    ap.add_argument("--show-text", action="store_true")
    args = ap.parse_args()
    urls = [u.strip() for u in open(args.urlfile, encoding="utf-8") if u.strip().startswith("http")]
    call = clf.make_gemini_caller()
    items = []
    for u in urls:
        page = clf.fetch_article(u)
        title = (page or {}).get("title", "") or u
        content = clf.build_content("", page)
        items.append({"company": "", "title": title, "content": content, "_url": u,
                      "_src": (page or {}).get("source", "fetch실패")})
    for i in range(0, len(items), 5):
        chunk = items[i:i + 5]
        res = clf.classify_items(chunk, call)
        for it, out in zip(chunk, res or [None] * len(chunk)):
            sent, info = out if out else ("(응답없음)", {})
            print("[%s] %s" % (sent if sent is not None else "제외", it["title"][:60]))
            print("   본문추출=%s(%d자) 유형=%s 사유=%s %s" % (
                it["_src"], len(it["content"]), info.get("event_type"), info.get("reason", ""), info.get("note", "")))
            print("   " + it["_url"])
            if args.show_text:
                print("   --- " + it["content"][:400])
    print("\n※ 위 결과 중 '제외/POSITIVE'가 아닌 NEGATIVE 가 남아 있으면 --show-text 결과와 함께 알려주세요.")


if __name__ == "__main__":
    main()
