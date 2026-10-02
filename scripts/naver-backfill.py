#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
naver_backfill.py — 네이버 뉴스 검색 API 기반 과거 기사 백필 (하루 상한 + 이어하기)

설계 요약
  1) 네이버 수집: 회사 x 키워드(기본 8개 + 확장 키워드) 조합을 sort=date 로 최대 1,000건씩 조회.
     pubDate 가 시작일보다 오래된 페이지에 도달하면 그 검색어는 조기 종료.
  2) Gemini 호출 전 사전 필터: 기간 / 제목 제외 패턴 / 유통사 나열 종합기사 / 제목 내 경쟁사 /
     이미 처리한 기사(상태 파일) / 유사기사 중복(기존 저장분 포함).
  3) Gemini 는 기사 N건을 한 번에 판별(배치). 하루 호출 상한(--daily-cap)에 도달하면 저장 후 종료.
  4) 처리한 기사는 docs/data/backfill_state.json 에 기록 -> 다음 실행은 자동으로 이어서 진행.

실행 예
  # (A) 진단: Gemini 호출 없음. 네이버가 어디까지 닿는지, 몇 번 호출이 필요한지 추정
  python scripts/naver_backfill.py --start 2024-01-01 --diagnose

  # (B) 본 실행: 하루 Gemini 호출 300회까지만
  python scripts/naver_backfill.py --start 2024-01-01 --daily-cap 300 --batch-size 8
"""
import os
import sys
import re
import json
import time
import math
import hashlib
import argparse
from datetime import datetime, timedelta, date
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor

import requests

# esg_collector 가 import 시점에 환경변수를 요구하므로, 이 스크립트에서 쓰지 않는 값은 더미로 채움
os.environ.setdefault("GMAIL_USER", "dummy@example.com")
os.environ.setdefault("GMAIL_APP_PASSWORD", "dummy")
if "--diagnose" in sys.argv:
    os.environ.setdefault("GEMINI_API_KEY", "dummy")
# 무료 API 안전 간격 (분당 약 10회). 필요하면 환경변수로 덮어쓰기
os.environ.setdefault("GEMINI_MIN_INTERVAL", "6")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import esg_collector as ec  # noqa: E402

# ---- 메일 발송 완전 차단 (백필은 수신자에게 리포팅하지 않음) ----
# 1) 수신자 목록 비움  2) SMTP 연결 자체를 예외 처리  3) 워크플로에도 Gmail 실계정 미전달
import smtplib  # noqa: E402

ec.RECIPIENTS = []


def _blocked_smtp(*_a, **_k):
    raise RuntimeError("백필 스크립트에서는 메일 발송이 차단되어 있습니다.")


smtplib.SMTP = _blocked_smtp
smtplib.SMTP_SSL = _blocked_smtp
print("[안전] 메일 발송 차단됨 (수신자 0명, SMTP 비활성)")

KST = ec.KST
STATE_FILENAME = "backfill_state.json"
MAX_ATTEMPTS = 3  # Gemini 응답에서 반복적으로 누락되는 기사는 3회 후 포기

# 기존 8개 키워드(ESG_KEYWORDS)는 긍정 성격이 강해, 부정 이슈를 잡는 키워드를 추가
EXTRA_KEYWORDS = [
    "친환경", "플라스틱", "재활용", "봉사", "후원", "협약",
    "갑질", "불공정", "과징금", "개인정보", "산업재해", "안전사고",
]


# ---------------------------------------------------------------- 유틸
def h(link):
    return hashlib.sha1(link.encode("utf-8")).hexdigest()[:16]


def parse_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def detect_company(title):
    """제목에서 가장 앞에 나오는 경쟁사(동일 위치면 더 긴 이름 우선)."""
    best = None
    for name in ec.COMPETITOR_NAMES:
        pos = title.find(name)
        if pos < 0:
            continue
        key = (pos, -len(name))
        if best is None or key < best[0]:
            best = (key, name)
    return best[1] if best else None


def title_excluded(title):
    for p in ec.TITLE_EXCLUDE_PATTERNS:
        if re.search(p, title):
            return True
    return False


# ---------------------------------------------------------------- 네이버 수집
def naver_get(params):
    headers = {
        "X-Naver-Client-Id": ec.NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": ec.NAVER_CLIENT_SECRET,
    }
    url = "https://openapi.naver.com/v1/search/news.json"
    backoff = 2
    for attempt in range(4):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=15)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(backoff)
                backoff *= 2
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            if attempt == 3:
                print("    [네이버 오류] " + str(e)[:80])
                return None
            time.sleep(backoff)
            backoff *= 2
    return None


def naver_search(query, start_date, end_date):
    """반환: (기간 내 기사 리스트, 조회한 전체 건수, 가장 오래된 기사 날짜)"""
    out, total, oldest = [], 0, None
    for start in range(1, ec.NAVER_MAX_START + 1, 100):
        data = naver_get({"query": query, "display": 100, "start": start, "sort": "date"})
        if not data:
            break
        items = data.get("items", [])
        if not items:
            break
        page_oldest = None
        for it in items:
            pd = ec.parse_pub_date(it.get("pubDate", ""))
            if pd is None:
                continue
            d = pd.date()
            total += 1
            if page_oldest is None or d < page_oldest:
                page_oldest = d
            if oldest is None or d < oldest:
                oldest = d
            if start_date <= d <= end_date:
                out.append({
                    "date": d.strftime("%Y-%m-%d"),
                    "title": ec.clean_html(it.get("title", "")),
                    "description": ec.clean_html(it.get("description", "")),
                    "link": it.get("originallink") or it.get("link") or "",
                })
        if page_oldest is not None and page_oldest < start_date:
            break
        if len(items) < 100:
            break
        time.sleep(0.05)
    return out, total, oldest


def build_queries(extra):
    kws = list(ec.ESG_KEYWORDS) + (EXTRA_KEYWORDS if extra else [])
    return [(c, c + " " + k) for c in ec.COMPETITORS for k in kws]


def collect(start_date, end_date, extra, verbose=True):
    """모든 검색어를 돌려 후보를 모은다. 반환: (후보 dict[link]->cand, 회사별 통계)."""
    cands = {}
    stats = {}  # company -> {"oldest": date, "queries": n, "in_range": n}
    queries = build_queries(extra)
    for i, (company, q) in enumerate(queries, 1):
        items, total, oldest = naver_search(q, start_date, end_date)
        s = stats.setdefault(company, {"oldest": None, "queries": 0, "in_range": 0, "capped": 0})
        s["queries"] += 1
        s["in_range"] += len(items)
        if oldest is not None and (s["oldest"] is None or oldest < s["oldest"]):
            s["oldest"] = oldest
        # 1,000건을 다 채웠는데도 시작일에 못 닿았다 = 그 검색어는 더 과거를 못 봄
        if total >= ec.NAVER_MAX_START and oldest is not None and oldest > start_date:
            s["capped"] += 1
        for it in items:
            if it["link"] and it["link"] not in cands:
                cands[it["link"]] = it
        if verbose and i % 20 == 0:
            print("  [수집] 검색어 %d/%d 완료, 후보 %d건" % (i, len(queries), len(cands)))
    return cands, stats


def prefilter(cands):
    """제목/회사 기반 사전 필터. 반환: (통과 리스트, 사유별 카운트)"""
    cnt = {"title_excluded": 0, "multi_retailer": 0, "no_company": 0}
    ok = []
    for c in cands.values():
        t = c["title"]
        if title_excluded(t):
            cnt["title_excluded"] += 1
            continue
        if ec.is_multi_retailer_briefing(t):
            cnt["multi_retailer"] += 1
            continue
        comp = detect_company(t)
        if not comp:
            cnt["no_company"] += 1
            continue
        c["company"] = comp
        ok.append(c)
    return ok, cnt


# ---------------------------------------------------------------- 저장소(월별 JSON) / 상태
class Store:
    def __init__(self, out_dir):
        self.dir = out_dir
        self.months = {}      # "YYYY-MM" -> list[record]
        self.touched = set()
        os.makedirs(out_dir, exist_ok=True)
        for fn in os.listdir(out_dir):
            if re.match(r"^\d{4}-\d{2}\.json$", fn):
                try:
                    with open(os.path.join(out_dir, fn), encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, list):
                        self.months[fn[:-5]] = data
                except Exception as e:
                    print("  [경고] %s 읽기 실패: %s" % (fn, e))
        self.links = {r.get("link") for recs in self.months.values() for r in recs}
        # 유사기사 판정용 인덱스: company -> [{"norm","article","date"}]
        self.index = {}
        for recs in self.months.values():
            for r in recs:
                self._index(r)

    def _index(self, rec):
        self.index.setdefault(rec.get("company"), []).append({
            "norm": ec.normalize_title(rec.get("title", "")),
            "article": rec,
            "date": rec.get("date", ""),
        })

    def find_dup(self, company, title, d, window_days=3):
        """같은 회사 + 전후 window_days 일 이내 기사 중 유사한 것. 반환: (기존기사, 동일기사여부)"""
        try:
            dd = parse_date(d)
        except Exception:
            return None, False
        sub = []
        for e in self.index.get(company, []):
            try:
                if abs((parse_date(e["date"]) - dd).days) <= window_days:
                    sub.append(e)
            except Exception:
                continue
        art = ec.find_duplicate_article(title, sub)
        if art is None:
            return None, False
        same = SequenceMatcher(None, ec.normalize_title(title),
                               ec.normalize_title(art.get("title", ""))).ratio() >= 0.95
        return art, same

    def add(self, rec):
        mk = rec["date"][:7]
        self.months.setdefault(mk, []).append(rec)
        self.touched.add(mk)
        self.links.add(rec["link"])
        self._index(rec)

    def mark_touched(self, rec):
        self.touched.add(rec["date"][:7])

    def flush(self):
        for mk in sorted(self.touched):
            path = os.path.join(self.dir, mk + ".json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.months[mk], f, ensure_ascii=False, indent=2)
        if self.touched:
            months = sorted(m for m in self.months)
            manifest = {"months": months, "last_updated": datetime.now(KST).isoformat()}
            with open(os.path.join(self.dir, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)
        self.touched = set()


def load_state(out_dir):
    path = os.path.join(out_dir, STATE_FILENAME)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                s = json.load(f)
            s.setdefault("judged", {})
            s.setdefault("attempts", {})
            return s
        except Exception:
            pass
    return {"judged": {}, "attempts": {}}


def save_state(out_dir, state):
    state["updated"] = datetime.now(KST).isoformat()
    with open(os.path.join(out_dir, STATE_FILENAME), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


# ---------------------------------------------------------------- Gemini 배치 판별
def build_batch_prompt(batch):
    lines = [
        "당신은 유통업계 ESG 뉴스 클리핑 담당자입니다.",
        "아래 %d개 기사 각각에 대해 독립적으로 두 가지를 판별하세요." % len(batch),
        "  (1) include: '경쟁 유통사 ESG 동향 모니터링 대시보드'에 실을 만한 기사인가?",
        "      해당 회사의 환경·사회·지배구조(ESG) 활동이나 이슈가 기사의 핵심 주제일 때만 true.",
        "      단순 신제품/행사/할인/실적/주가/인사/광고성 기사는 false.",
        "  (2) label: 실을 만하다면 해당 회사에 유리한 소식이면 POSITIVE, 불리한 소식이면 NEGATIVE,",
        "      어느 쪽도 아니면 NEUTRAL. 실을 만하지 않으면 N/A.",
        "      표면적인 단어가 아니라 '누가 누구에게 무엇을 했는가'로 판단하세요.",
        "      (예: 소비자가 사죄금을 기부한 미담은 '사죄'라는 단어가 있어도 POSITIVE)",
        "",
        "===== 출력 형식 =====",
        "다음 형식의 JSON 배열만 반환하세요. 다른 텍스트는 절대 포함하지 마세요. 모든 id를 빠짐없이 포함하세요.",
        '[{"id": 1, "include": true|false, "label": "POSITIVE"|"NEGATIVE"|"NEUTRAL"|"N/A", "reason": "한 문장"}]',
        "",
    ]
    for i, c in enumerate(batch, 1):
        lines.append("[기사 %d]" % i)
        lines.append("회사: " + c["company"])
        lines.append("제목: " + c["title"])
        lines.append("내용: " + c["content"])
        lines.append("")
    return "\n".join(lines)


def parse_batch_response(raw):
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        m = re.search(r"\[[\s\S]*\]", raw)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except Exception:
            return None
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                data = v
                break
    if not isinstance(data, list):
        return None
    out = {}
    for row in data:
        if isinstance(row, dict):
            try:
                out[int(row.get("id"))] = row
            except Exception:
                continue
    return out


def prepare_content(c):
    body = ec.fetch_article_body(c["link"])
    if body and ec.is_paywalled(body):
        c["paywalled"] = True
        return c
    text = "요약: " + c["description"]
    if body:
        text += " / 본문: " + body[:800]
    c["content"] = text[:1200]
    return c


# ---------------------------------------------------------------- 메인
def main():
    ap = argparse.ArgumentParser(description="네이버 뉴스 과거 기사 백필 (하루 Gemini 호출 상한 + 이어하기)")
    ap.add_argument("--start", required=True, help="백필 시작일 YYYY-MM-DD")
    ap.add_argument("--end", default=None, help="백필 종료일 YYYY-MM-DD (기본: 오늘)")
    ap.add_argument("--daily-cap", type=int, default=int(os.environ.get("DAILY_CAP", "300")),
                    help="이번 실행에서 사용할 Gemini 호출 수 상한 (기본 300)")
    ap.add_argument("--batch-size", type=int, default=int(os.environ.get("BATCH_SIZE", "8")),
                    help="Gemini 1회 호출당 기사 수 (기본 8)")
    ap.add_argument("--max-minutes", type=float, default=float(os.environ.get("MAX_MINUTES", "300")),
                    help="이 시간(분)을 넘기면 안전하게 저장 후 종료")
    ap.add_argument("--no-extra-keywords", action="store_true", help="확장 키워드 없이 기본 8개 키워드만 사용")
    ap.add_argument("--diagnose", action="store_true", help="Gemini 호출 없이 도달 범위/필요 호출 수만 추정")
    ap.add_argument("--output-dir", default=ec.DASHBOARD_DATA_DIR)
    args = ap.parse_args()

    start_date = parse_date(args.start)
    end_date = parse_date(args.end) if args.end else ec.TODAY
    t0 = time.time()
    print("[시작] 백필 %s ~ %s | 일일 상한 %d회 x 배치 %d건 | 모드: %s" % (
        start_date, end_date, args.daily_cap, args.batch_size, "진단" if args.diagnose else "본 실행"))

    store = Store(args.output_dir)
    state = load_state(args.output_dir)
    judged, attempts = state["judged"], state["attempts"]
    print("[기존] 대시보드 기사 %d건, 처리 완료 기록 %d건" % (len(store.links), len(judged)))

    # 1) 네이버 수집
    cands, stats = collect(start_date, end_date, extra=not args.no_extra_keywords)
    print("[수집] 기간 내 후보 %d건 (중복 링크 제거 후)" % len(cands))

    # 2) 사전 필터
    passed, cnt = prefilter(cands)
    print("[사전필터] 제목패턴 제외 %d / 유통사 나열 제외 %d / 제목에 경쟁사 없음 %d -> 통과 %d" % (
        cnt["title_excluded"], cnt["multi_retailer"], cnt["no_company"], len(passed)))
    todo = [c for c in passed
            if c["link"] not in store.links
            and h(c["link"]) not in judged
            and attempts.get(h(c["link"]), 0) < MAX_ATTEMPTS]
    todo.sort(key=lambda c: c["date"])
    print("[대기] 아직 처리하지 않은 기사 %d건" % len(todo))

    est_calls = math.ceil(len(todo) / max(1, args.batch_size))
    est_days = math.ceil(est_calls / max(1, args.daily_cap)) if est_calls else 0
    print("[추정] 유사기사 병합 전 상한 기준 Gemini 호출 약 %d회 -> 일일 상한 %d회면 최대 %d일" % (
        est_calls, args.daily_cap, est_days))

    if args.diagnose:
        print("\n[회사별 도달 범위]  (capped = 1,000건을 다 채우고도 시작일에 못 닿은 검색어 수)")
        print("%-10s %-12s %8s %8s %s" % ("회사", "가장 오래된 기사", "기간내", "검색어", "capped"))
        for comp in ec.COMPETITORS:
            s = stats.get(comp)
            if not s:
                continue
            print("%-10s %-12s %8d %8d %s" % (
                comp, str(s["oldest"]) if s["oldest"] else "-", s["in_range"], s["queries"], s["capped"]))
        print("\n'가장 오래된 기사'가 시작일보다 늦고 capped 가 크면, 그 회사는 네이버만으로 시작일까지 닿지 못합니다.")
        return

    if not todo:
        print("[완료] 처리할 기사가 남아 있지 않습니다. (BACKFILL_DONE)")
        return

    # 3) 배치 판별
    calls, fails_in_row, new_rec, merged, rejected = 0, 0, 0, 0, 0
    stop_reason = None
    pool = ThreadPoolExecutor(max_workers=8)
    pending = []

    def process_batch(batch):
        nonlocal calls, fails_in_row, new_rec, merged, rejected
        batch = list(pool.map(prepare_content, batch))
        usable = []
        for c in batch:
            if c.get("paywalled"):
                judged[h(c["link"])] = "r"
                rejected += 1
            else:
                usable.append(c)
        if not usable:
            return True
        raw = ec._call_gemini(build_batch_prompt(usable), timeout=120)
        calls += 1
        res = parse_batch_response(raw)
        if res is None:
            fails_in_row += 1
            for c in usable:
                attempts[h(c["link"])] = attempts.get(h(c["link"]), 0) + 1
            print("  [배치 실패] 연속 %d회" % fails_in_row)
            return fails_in_row < 2  # 연속 2회 실패 = 일일 쿼터 소진 등으로 보고 중단
        fails_in_row = 0
        for i, c in enumerate(usable, 1):
            key = h(c["link"])
            row = res.get(i)
            if row is None:
                attempts[key] = attempts.get(key, 0) + 1
                continue
            label = str(row.get("label", "")).upper()
            if not row.get("include") or label in ("NEUTRAL", "N/A", ""):
                judged[key] = "r"
                rejected += 1
                continue
            sentiment = label if label in ("POSITIVE", "NEGATIVE") else "UNCERTAIN"
            art, same = store.find_dup(c["company"], c["title"], c["date"])
            judged[key] = "a"
            if art is not None:
                if not same:
                    art["related_count"] = int(art.get("related_count", 1)) + 1
                    store.mark_touched(art)
                    merged += 1
                continue
            store.add({
                "date": c["date"], "company": c["company"], "sentiment": sentiment,
                "title": c["title"], "description": c["description"],
                "link": c["link"], "related_count": 1,
            })
            new_rec += 1
        store.flush()
        save_state(args.output_dir, state)
        print("  [진행] 호출 %d/%d회, 신규 %d건, 병합 %d건, 제외 %d건" % (
            calls, args.daily_cap, new_rec, merged, rejected))
        return True

    def over_budget():
        if calls >= args.daily_cap:
            return "일일 호출 상한 도달"
        if (time.time() - t0) / 60 >= args.max_minutes:
            return "최대 실행 시간 도달"
        return None

    try:
        for c in todo:
            # Gemini 보내기 전 유사기사 선처리 (호출 절약)
            art, same = store.find_dup(c["company"], c["title"], c["date"])
            if art is not None:
                judged[h(c["link"])] = "d"
                if not same:
                    art["related_count"] = int(art.get("related_count", 1)) + 1
                    store.mark_touched(art)
                    merged += 1
                continue
            pending.append(c)
            if len(pending) >= args.batch_size:
                stop_reason = over_budget()
                if stop_reason:
                    break
                if not process_batch(pending):
                    stop_reason = "Gemini 연속 실패(쿼터 소진 추정)"
                    pending = []
                    break
                pending = []
        else:
            if pending:
                stop_reason = over_budget()
                if not stop_reason and not process_batch(pending):
                    stop_reason = "Gemini 연속 실패(쿼터 소진 추정)"
    except KeyboardInterrupt:
        stop_reason = "사용자 중단(Ctrl+C)"
    finally:
        pool.shutdown(wait=False)
        store.flush()
        save_state(args.output_dir, state)

    remaining = sum(1 for c in todo if h(c["link"]) not in judged)
    print("\n" + "=" * 60)
    print("[결과] 호출 %d회 | 신규 %d건 | 유사기사 병합 %d건 | 제외 %d건 | 소요 %.1f분" % (
        calls, new_rec, merged, rejected, (time.time() - t0) / 60))
    print("[종료 사유] " + (stop_reason or "대기 기사 모두 처리"))
    print("[남은 기사] 약 %d건 -> %s" % (
        remaining, "다음 실행에서 이어서 처리" if remaining else "백필 완료 (BACKFILL_DONE)"))


if __name__ == "__main__":
    main()
