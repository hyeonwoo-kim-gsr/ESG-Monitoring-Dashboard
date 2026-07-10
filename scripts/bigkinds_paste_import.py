"""
빅카인즈 붙여넣기(TSV) → Gemini 필터링 → 대시보드 JSON 병합

esg_collector.py 와 동일한 분석 로직(analyze_article, screen_relevance,
방향성 판별, 유사 기사 중복 판정, 유통사 다수 나열 감지 등)을 재사용해서,
매일 자동 수집과 백필의 판별 기준을 완전히 일치시킨다.

동작 방식:
1. `docs/data/bigkinds_paste.tsv` 를 읽어 붙여넣기 영역의 데이터 행을 파싱
2. 각 행에 대해 esg_collector 의 필터 체인을 순서대로 적용
   - 제목 패턴 필터 (유통레이더/브리핑/등)
   - 유통사 3개 이상 나열 종합 기사 감지
   - 유사 기사 중복 제거
   - Gemini 관련성/감성 판별
3. 통과한 기사만 `docs/data/YYYY-MM.json` 에 병합
4. manifest.json 갱신
5. 처리 완료된 tsv 파일의 붙여넣기 영역을 비움 (다음 배치 준비)

실행 방법:
    python scripts/bigkinds_paste_import.py
    python scripts/bigkinds_paste_import.py --dry-run  # 저장 없이 결과만 확인
    python scripts/bigkinds_paste_import.py --keep-paste  # 처리 후 파일 비우지 않음
"""

import os
import re
import sys
import json
import argparse
from datetime import datetime
from zoneinfo import ZoneInfo

# esg_collector.py 의 함수들을 그대로 재사용
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import esg_collector as ec

KST = ZoneInfo("Asia/Seoul")
PASTE_FILE = os.environ.get("BIGKINDS_PASTE_FILE", "docs/data/bigkinds_paste.tsv")
DASHBOARD_DATA_DIR = os.environ.get("DASHBOARD_DATA_DIR", "docs/data")

# 빅카인즈 표준 컬럼 순서 (2026년 기준)
# 이 순서와 다르면 column_index 를 수정하세요.
BIGKINDS_COLUMNS = [
    "뉴스식별자", "일자", "언론사", "기고자", "제목",
    "통합분류1", "통합분류2", "통합분류3",
    "사건사고분류1", "사건사고분류2", "사건사고분류3",
    "인물", "위치", "기관", "키워드", "특성추출",
    "본문", "URL", "분석제외여부",
]

# 핵심 컬럼 인덱스 (빅카인즈가 컬럼 순서를 바꾸면 여기만 수정하면 됨)
COL_DATE_IDX  = 1     # 일자
COL_MEDIA_IDX = 2     # 언론사
COL_TITLE_IDX = 4     # 제목
COL_BODY_IDX  = 16    # 본문
COL_URL_IDX   = 17    # URL

PASTE_MARKER = "# === 여기부터 붙여넣기 ==="


def read_paste_rows(path):
    """붙여넣기 파일에서 데이터 행만 추출.
    반환: (data_lines, header_lines) — header_lines 는 마커까지 포함한 파일 앞부분.
    """
    if not os.path.exists(path):
        print(f"[오류] 붙여넣기 파일이 없습니다: {path}")
        return [], []

    with open(path, encoding="utf-8") as f:
        all_lines = f.readlines()

    header_lines = []
    data_lines = []
    in_data = False
    for line in all_lines:
        if not in_data:
            header_lines.append(line)
            if PASTE_MARKER in line:
                in_data = True
            continue
        # 데이터 영역: 빈 줄이나 주석은 건너뛰고, 실제 탭 구분된 행만 수집
        stripped = line.rstrip("\n")
        if not stripped.strip():
            continue
        if stripped.lstrip().startswith("#"):
            continue
        data_lines.append(stripped)

    return data_lines, header_lines


def parse_row(line):
    """탭 구분된 한 줄을 dict 로 파싱.
    (엑셀에서 복사할 때 셀 안 줄바꿈은 공백으로 대체됨을 전제)
    """
    parts = line.split("\t")
    if len(parts) < COL_URL_IDX + 1:
        return None
    return {
        "date_raw": parts[COL_DATE_IDX].strip() if len(parts) > COL_DATE_IDX else "",
        "media":    parts[COL_MEDIA_IDX].strip() if len(parts) > COL_MEDIA_IDX else "",
        "title":    parts[COL_TITLE_IDX].strip() if len(parts) > COL_TITLE_IDX else "",
        "body":     parts[COL_BODY_IDX].strip()  if len(parts) > COL_BODY_IDX  else "",
        "url":      parts[COL_URL_IDX].strip()   if len(parts) > COL_URL_IDX   else "",
    }


def normalize_date(s):
    """빅카인즈 일자 컬럼(20200315, 2020-03-15, 2020.03.15 등)을 YYYY-MM-DD 로."""
    if not s:
        return None
    s = re.sub(r"[.\-/\s]", "", s)[:8]
    if len(s) != 8 or not s.isdigit():
        return None
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def detect_company(title, body):
    """제목·본문 앞부분에서 경쟁사 이름 감지. esg_collector 의 COMPETITOR_NAMES 사용."""
    text = (title or "") + " " + (body or "")[:200]
    for name in ec.COMPETITOR_NAMES:
        if name in text:
            return name
    return None


def load_existing_urls(output_dir):
    """이미 저장된 모든 월 파일의 URL 집합을 반환 (중복 스킵용)."""
    urls = set()
    if not os.path.isdir(output_dir):
        return urls
    for fname in os.listdir(output_dir):
        if not re.match(r"^\d{4}-\d{2}\.json$", fname):
            continue
        with open(os.path.join(output_dir, fname), encoding="utf-8") as f:
            for art in json.load(f):
                if art.get("link"):
                    urls.add(art["link"])
    return urls


def save_records(records, output_dir):
    """월별 JSON 에 URL 기준 중복 없이 병합. esg_collector.export_dashboard_data 와 동일한 스키마."""
    os.makedirs(output_dir, exist_ok=True)
    by_month = {}
    for rec in records:
        by_month.setdefault(rec["date"][:7], []).append(rec)

    touched = set()
    for mk, new_recs in by_month.items():
        path = os.path.join(output_dir, mk + ".json")
        existing = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                existing = json.load(f)
        by_link = {a["link"]: a for a in existing}
        added = 0
        for rec in new_recs:
            if rec["link"] not in by_link:
                existing.append(rec)
                by_link[rec["link"]] = rec
                added += 1
        with open(path, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        touched.add(mk)
        print(f"  [저장] {path}  +{added}건 (누계 {len(existing)}건)")

    # manifest.json 갱신
    manifest_path = os.path.join(output_dir, "manifest.json")
    all_months = set(touched)
    for fname in os.listdir(output_dir):
        if re.match(r"^\d{4}-\d{2}\.json$", fname):
            all_months.add(fname.replace(".json", ""))
    manifest = {
        "months": sorted(all_months),
        "last_updated": datetime.now(KST).isoformat(),
    }
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"  [manifest] {manifest_path} 갱신 ({len(all_months)}개월)")


def clear_paste_area(path, header_lines):
    """처리 완료된 파일의 붙여넣기 영역을 비운다 (헤더/사용법 주석만 남김)."""
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(header_lines)
    print(f"  [초기화] {path} 붙여넣기 영역 비움 (다음 배치 준비 완료)")


def main():
    parser = argparse.ArgumentParser(description="빅카인즈 붙여넣기 데이터를 Gemini 로 필터링해 대시보드에 병합")
    parser.add_argument("--dry-run", action="store_true", help="실제 저장 없이 결과만 출력")
    parser.add_argument("--keep-paste", action="store_true", help="처리 완료 후 붙여넣기 영역을 비우지 않음")
    parser.add_argument("--file", default=None, help="붙여넣기 파일 경로 (기본: BIGKINDS_PASTE_FILE 또는 docs/data/bigkinds_paste.tsv)")
    args = parser.parse_args()

    paste_file = args.file or PASTE_FILE
    print(f"[시작] {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')} 빅카인즈 배치 처리")
    print(f"[설정] 붙여넣기 파일: {paste_file}")
    print(f"[설정] 출력 경로: {DASHBOARD_DATA_DIR}")

    data_lines, header_lines = read_paste_rows(paste_file)
    if not data_lines:
        print("[완료] 붙여넣기 영역이 비어 있어 처리할 기사가 없습니다. 정상 종료.")
        return

    print(f"[읽음] 붙여넣기 영역에서 {len(data_lines)}행 감지")

    existing_urls = load_existing_urls(DASHBOARD_DATA_DIR)
    print(f"[기존] 대시보드에 이미 저장된 기사 {len(existing_urls)}건 (중복 스킵 기준)")

    accepted_index = []
    records_to_save = []
    stats = {"total": 0, "malformed": 0, "no_date": 0, "no_url": 0, "no_company": 0,
             "title_excluded": 0, "multi_retailer": 0, "duplicate": 0,
             "already_saved": 0, "gemini_excluded": 0, "gemini_failed": 0,
             "positive": 0, "negative": 0}

    for line in data_lines:
        stats["total"] += 1
        row = parse_row(line)
        if row is None:
            stats["malformed"] += 1
            print(f"  [형식 오류] 컬럼 수 부족: {line[:60]}…")
            continue

        date_str = normalize_date(row["date_raw"])
        if not date_str:
            stats["no_date"] += 1
            print(f"  [날짜 없음] {row['title'][:50]}")
            continue

        title = row["title"]
        body  = row["body"]
        url   = row["url"].strip() if row["url"] else ""

        # URL 없는 기사는 대시보드에 반영하지 않음
        # (원본 확인 링크가 없으면 사용자가 검증할 수 없어 대시보드 신뢰도가 떨어짐)
        if not url or not url.lower().startswith(("http://", "https://")):
            stats["no_url"] += 1
            print(f"  [URL 없음] 반영 제외 -> {title[:50]}")
            continue

        # 이미 대시보드에 있는 기사는 스킵
        if url in existing_urls:
            stats["already_saved"] += 1
            print(f"  [기존 저장] 스킵 -> {title[:50]}")
            continue

        # 1차: esg_collector 의 제목 패턴 필터
        if ec.is_excluded_by_title(title):
            stats["title_excluded"] += 1
            continue

        # 1.5차: 유통사 3개 이상 나열 자동 제외
        if ec.is_multi_retailer_briefing(title):
            n = ec.count_retailers_in_title(title)
            print(f"  [종합 기사] 유통사 {n}개 나열 제외 -> {title[:50]}")
            stats["multi_retailer"] += 1
            continue

        # 2차: 경쟁사 이름 감지
        company = detect_company(title, body)
        if not company:
            stats["no_company"] += 1
            print(f"  [경쟁사 없음] {title[:50]}")
            continue

        # 3차: 유사 기사 중복 판정 (이번 배치 안에서)
        dup = ec.find_duplicate_article(title, accepted_index)
        if dup is not None:
            dup["related_count"] = dup.get("related_count", 1) + 1
            print(f"  [유사 기사] 동일 주제 누적 -> {title[:50]} (관련 보도 {dup['related_count']}건)")
            stats["duplicate"] += 1
            continue

        # 4차: Gemini 관련성/감성 판별 (esg_collector.analyze_article 그대로 재사용)
        # bigkinds 본문이 이미 있으므로 fetch_article_body 는 스킵되고 body 가 그대로 사용됨
        sentiment = ec.analyze_article(title, body, url)

        if sentiment is None:
            stats["gemini_excluded"] += 1
            continue

        if sentiment == "UNCERTAIN":
            stats["gemini_failed"] += 1
            # UNCERTAIN 도 저장 (매일 수집과 동일한 정책)

        record = {
            "date":          date_str,
            "company":       company,
            "sentiment":     sentiment,
            "title":         title,
            "description":   body[:200] + "…" if len(body) > 200 else body,
            "link":          url,
            "related_count": 1,
        }
        records_to_save.append(record)
        accepted_index.append({
            "norm":    ec.normalize_title(title),
            "article": record,
        })
        if sentiment == "POSITIVE":
            stats["positive"] += 1
        elif sentiment == "NEGATIVE":
            stats["negative"] += 1

    # 결과 저장
    print("\n" + "=" * 60)
    print("[배치 처리 결과]")
    print("=" * 60)
    print(f"  전체 행수:               {stats['total']:>5}")
    print(f"  ├ 형식 오류:             {stats['malformed']:>5}")
    print(f"  ├ 날짜 파싱 실패:        {stats['no_date']:>5}")
    print(f"  ├ URL 없음:              {stats['no_url']:>5}")
    print(f"  ├ 이미 대시보드에 있음:  {stats['already_saved']:>5}")
    print(f"  ├ 제목 패턴 제외:        {stats['title_excluded']:>5}")
    print(f"  ├ 유통사 나열 종합기사:  {stats['multi_retailer']:>5}")
    print(f"  ├ 경쟁사 이름 없음:      {stats['no_company']:>5}")
    print(f"  ├ 유사 기사 중복:        {stats['duplicate']:>5}")
    print(f"  ├ Gemini 관련성 없음:    {stats['gemini_excluded']:>5}")
    print(f"  └ Gemini 판별 실패:      {stats['gemini_failed']:>5}")
    print(f"  ─────────────────────────────")
    print(f"  대시보드 반영 대상:      {len(records_to_save):>5}건")
    print(f"    (긍정 {stats['positive']}, 부정 {stats['negative']}, 검토필요 {stats['gemini_failed']})")

    if args.dry_run:
        print("\n[dry-run] 실제 저장은 하지 않았습니다.")
        return

    if records_to_save:
        save_records(records_to_save, DASHBOARD_DATA_DIR)

    if not args.keep_paste:
        clear_paste_area(paste_file, header_lines)

    print(f"\n[완료] {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
