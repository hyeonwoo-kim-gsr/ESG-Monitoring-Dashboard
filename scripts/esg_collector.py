import os
import re
import json
import time
import smtplib
import requests
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import unescape
from zoneinfo import ZoneInfo

KST   = ZoneInfo("Asia/Seoul")
TODAY = datetime.now(KST).date()

# 스크리닝 범위 설정
#   SCREENING_MODE=recent (기본값) : LOOKBACK_DAYS 만큼만 본다 (기존 동작, 매일 자동 실행에 적합)
#   SCREENING_MODE=full           : 날짜로 거르지 않고, Naver 검색 API가 쿼리당 내려주는
#                                    최대 깊이(1000건)까지 페이지네이션해서 전부 스크리닝한다.
#                                    (네이버 API 자체에 "기간" 파라미터가 없어서, "모든 기간"은
#                                    정확히는 "API가 보여줄 수 있는 가장 깊은 범위까지"를 의미함)
SCREENING_MODE = os.environ.get("SCREENING_MODE", "recent").lower()
LOOKBACK_DAYS  = int(os.environ.get("LOOKBACK_DAYS", "3"))
YESTERDAY      = TODAY - timedelta(days=LOOKBACK_DAYS)
NAVER_MAX_START = 1000  # Naver 검색 API의 쿼리당 최대 조회 깊이

RECIPIENTS = ["fahudkim@gsretail.com", "rang428@gsretail.com", "kyahn@gsretail.com", "jsyou@gsretail.com", "lhj1120@gsretail.com"]

# 대시보드 데이터 출력 위치 (GitHub Pages: 저장소 settings > Pages > Source = main / docs)
DASHBOARD_DATA_DIR = os.environ.get("DASHBOARD_DATA_DIR", "docs/data")

COMPETITORS = [
    "GS리테일", "BGF", "세븐일레븐", "이마트24", "이마트", "이마트에브리데이",
    "롯데슈퍼", "롯데마트", "롯데홈쇼핑", "현대홈쇼핑", "CJ온스타일", "롯데백화점",
    "현대백화점", "신세계백화점",
]

COMPETITOR_NAMES = [
    "GS리테일", "BGF", "세븐일레븐", "이마트24", "이마트", "이마트에브리데이",
    "롯데슈퍼", "롯데마트", "롯데홈쇼핑", "현대홈쇼핑", "CJ온스타일", "롯데백화점",
    "현대백화점", "신세계백화점",
]

ESG_KEYWORDS = [
    "ESG", "지속가능성", "ISO", "사회공헌", "CSR",
    "탄소중립", "기후변화", "기후대응", "지속가능경영",
    "탄소배출", "탄소절감", "탄소저감", "상생", "사회적책임",
    "동반성장", "지배구조", "기부",
]

PAYWALL_MARKERS = [
    "유료 기사", "유료기사", "구독 후 이용", "구독하시면",
    "로그인 후 이용", "회원 전용", "프리미엄 기사",
    "이 기사는 유료", "구독권이 필요",
    "subscribe to read", "premium content", "subscribers only",
]

TITLE_EXCLUDE_PATTERNS = [
    # 합본·모음·브리핑 기사
    r"신상브리핑",
    r"신상.*출시",
    r"오늘의 유통",
    r"오늘의 겜",
    r"오늘의 브랜드",
    r"오늘의 .{1,6}업",
    r"경제소식",
    r".{1,6}업계 소식",
    r".{1,6}소식\]",
    r"글로벌 레이더",
    r"유통 레이더",
    r"유통레이더",
    r"유통소식",
    r"유통단신",
    r"유통[/·]식음료",
    r"\[유통가\]",
    r"유통가[,\s]*<",
    r"유통가[,\s]*키",
    r"산업소식",
    r"IT 투데이",
    r"잘먹잘살",
    r"더밸류 브리핑",
    r"뉴스브리핑",
    r"브랜드 콜라보",
    r"기업家",
    r"겜업",
    r"게임 ON",
    r"위클리",
    r"주간.*뉴스",
    r"뉴스.*모음",
    r"이모저모",
    r"미리보는.*신문",
    r"신문.*미리보기",
    r"여기 유통",
    r"A오늘의",
    r"Car & Now",
    r"가전 트렌드",
    r"트렌드\]",
    # 外 패턴
    r"外",
    r"외\s*[<\],]",
    r"외\s*$",
    r"^\[.*\].*외",
    # 증권·금융 관련
    r"코스피", r"코스닥", r"주요공시", r"목표주가",
    r"주가조작", r"상장폐지", r"유상증자",
    r"증권.*뉴스", r"주주연대", r"탄원서", r"금감원",
    # 부동산·건설
    r"분양", r"아파트", r"대단지", r"입주",
    r"주거 지형", r"메가센텀", r"재개발", r"재건축",
    # 선거·정치
    r"후보", r"출마", r"공약", r"선거",
    # 기타 무관
    r"로또", r"당첨", r"게임.*IP",
    r"노사.*담판", r"파업", r"총파업",
    r"어패럴", r"패션.*뉴스", r"자동차.*뉴스",
    r"재벌.*취미", r"취미.*생활",
    r"어린이.*도서관", r"월드컵.*중계",
    r"모내기", r"농산업",
    r"벤처스",
]

NAVER_CLIENT_ID     = os.environ["NAVER_CLIENT_ID"]
NAVER_CLIENT_SECRET = os.environ["NAVER_CLIENT_SECRET"]
GMAIL_USER          = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD  = os.environ["GMAIL_APP_PASSWORD"]
GEMINI_API_KEY      = os.environ["GEMINI_API_KEY"]

if SCREENING_MODE == "full":
    print("[설정] 오늘: " + str(TODAY) + ", 스크리닝 모드: full (기간 제한 없음, 쿼리당 최대 " + str(NAVER_MAX_START) + "건)")
    print("[안내] full 모드는 호출량이 크게 늘어납니다 (Naver API 호출 수 + Gemini 분석 비용 모두 증가).")
    print("[안내] 1회성 백필 목적이라면, 백필 후에는 SCREENING_MODE=recent 로 되돌리는 것을 권장합니다.")
else:
    print("[설정] 오늘: " + str(TODAY) + ", 스크리닝 모드: recent (최근 " + str(LOOKBACK_DAYS) + "일: " + str(YESTERDAY) + " ~ " + str(TODAY) + ")")
print("[설정] 수신자: " + str(RECIPIENTS))
print("[설정] 대시보드 데이터 출력 경로: " + DASHBOARD_DATA_DIR)


def clean_html(text):
    text = re.sub(r"<[^>]+>", "", text)
    return unescape(text).strip()


def parse_pub_date(date_str):
    try:
        return datetime.strptime(date_str, "%a, %d %b %Y %H:%M:%S %z").astimezone(KST)
    except Exception:
        return None


def is_recent(pub_date):
    if pub_date is None:
        return False
    if SCREENING_MODE == "full":
        return True
    return YESTERDAY <= pub_date.date() <= TODAY


def is_excluded_by_title(title):
    for pattern in TITLE_EXCLUDE_PATTERNS:
        if re.search(pattern, title):
            print("    [제목 필터] 제외 -> " + title[:50])
            return True
    return False


def title_has_competitor(title):
    for name in COMPETITOR_NAMES:
        if name in title:
            return True
    return False


def normalize_title(title):
    """유사도 비교를 위한 제목 정규화 (괄호·특수문자·공백 제거)"""
    t = re.sub(r"\[.*?\]", "", title)        # 대괄호 머리말 제거
    t = re.sub(r"[^\w가-힣]", "", t)          # 특수문자 제거
    return t.lower()


def find_duplicate_article(title, accepted_index, threshold=0.6):
    """이미 수집된 기사 중 유사 기사를 찾아 그 기사 dict를 반환 (없으면 None).
    동일 주제로 여러 매체가 보도한 경우 원본 기사의 related_count를 누적시키기 위해 사용.
    """
    norm = normalize_title(title)
    for entry in accepted_index:
        ratio = SequenceMatcher(None, norm, entry["norm"]).ratio()
        if ratio >= threshold:
            return entry["article"]
    return None


def fetch_article_body(article_url):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(article_url, headers=headers, timeout=10)
        r.raise_for_status()
        text = re.sub(r"<[^>]+>", " ", r.text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:2000]
    except Exception as e:
        print("    [본문 크롤링 실패] " + str(e))
        return ""


def is_paywalled(body_text):
    lower = body_text.lower()
    for marker in PAYWALL_MARKERS:
        if marker.lower() in lower:
            return True
    return False


def analyze_article(title, description, article_url):
    """Gemini로 관련성 + 긍정/부정을 한 번에 판별
    반환: "POSITIVE" / "NEGATIVE" / None(제외)
    """
    gemini_url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.0-flash:generateContent?key=" + GEMINI_API_KEY
    )

    body_text = fetch_article_body(article_url)

    if body_text and is_paywalled(body_text):
        print("    [유료 기사] 제외 -> " + title[:40])
        return None

    content = body_text if body_text else description
    if not body_text:
        print("    [본문 없음] description으로 대체 판단 -> " + title[:40])

    prompt = "\n".join([
        "아래 뉴스 기사를 읽고 네 가지 조건을 모두 검토하세요.",
        "",
        "[조건 1] 기사의 핵심 주체가 아래 경쟁사 중 하나여야 합니다.",
        "경쟁사 목록: GS리테일, BGF리테일, 세븐일레븐, 코리아세븐, 이마트24, 이마트에브리데이,",
        "이마트, 롯데쇼핑, 롯데슈퍼, 롯데마트, 롯데홈쇼핑, 현대홈쇼핑, CJ온스타일,",
        "롯데백화점, 현대백화점, 신세계백화점",
        "",
        "조건 1 실패 사례:",
        "- 기사 주인공이 경쟁사가 아닌 다른 회사 단체 개인임",
        "- 경쟁사가 판매채널 입점처 유통채널로만 언급됨",
        "- 경쟁사가 비교 배경으로만 등장함",
        "- 모회사 계열사 기사에서 경쟁사가 일부만 언급됨",
        "- 여러 기업을 나열한 합본 요약 기사",
        "- 증권사 분석 주가 목표주가 공시 기사",
        "- 선거 정치 부동산 노사 파업 자동차 패션 농업 방송 등 무관 주제 기사",
        "",
        "[조건 2] 기사 전체 텍스트 중 해당 경쟁사의 ESG 활동 내용이 30% 이상을 차지해야 합니다.",
        "ESG 활동 예시: 탄소중립, 친환경 포장재, 사회공헌, 협력사 상생,",
        "지배구조 개선, 지속가능경영 보고서, 재생에너지, 공급망 관리 등",
        "",
        "[조건 3] 해외 법인 해외 시장만을 다루는 기사는 제외합니다.",
        "",
        "[조건 4] 단일 주제를 깊이 다루는 실제 뉴스 기사여야 합니다.",
        "(여러 단신 나열, 광고성 보도자료 제외)",
        "",
        "답변 규칙 (이 중 하나만 답하세요. 다른 말은 절대 하지 마세요):",
        "- 네 조건을 모두 충족하고 기사 내용이 긍정적(수상, 성과, 활동 확대, 협약, 모범 사례 등)이면: POSITIVE",
        "- 네 조건을 모두 충족하고 기사 내용이 부정적(논란, 비판, 제재, 위반, 사고, 갑질, 불매 등)이면: NEGATIVE",
        "- 조건을 하나라도 충족하지 못하면: NO",
        "",
        "제목: " + title,
        "본문: " + content,
    ])

    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    try:
        r = requests.post(gemini_url, json=payload, timeout=15)
        r.raise_for_status()
        answer = (
            r.json()["candidates"][0]["content"]["parts"][0]["text"]
            .strip()
            .upper()
        )
        if answer.startswith("POSITIVE"):
            print("    [Gemini] 포함(긍정) -> " + title[:40])
            return "POSITIVE"
        if answer.startswith("NEGATIVE"):
            print("    [Gemini] 포함(부정) -> " + title[:40])
            return "NEGATIVE"
        print("    [Gemini] 제외 -> " + title[:40])
        return None
    except Exception as e:
        print("    [Gemini 오류] " + str(e) + " -> 기본값 긍정 포함 처리")
        return "POSITIVE"


def search_news(company, keyword, display=5):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {
        "X-Naver-Client-Id":     NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }

    if SCREENING_MODE != "full":
        params = {"query": company + " " + keyword, "display": display, "sort": "date"}
        try:
            r = requests.get(url, headers=headers, params=params, timeout=10)
            r.raise_for_status()
            items = r.json().get("items", [])
            print("  [Naver] " + company + " + " + keyword + " -> " + str(len(items)) + "건")
            return items
        except Exception as e:
            print("  [Naver 오류] " + company + " / " + keyword + ": " + str(e))
            return []

    # SCREENING_MODE == "full": 날짜로 거르지 않는 대신, 한 쿼리당 Naver가 허용하는
    # 최대 깊이(1000건)까지 start를 옮겨가며 끝까지 수집한다.
    all_items = []
    start = 1
    page_display = 100
    while start <= NAVER_MAX_START:
        params = {"query": company + " " + keyword, "display": page_display, "start": start, "sort": "date"}
        try:
            r = requests.get(url, headers=headers, params=params, timeout=10)
            r.raise_for_status()
            items = r.json().get("items", [])
        except Exception as e:
            print("  [Naver 오류] " + company + " / " + keyword + " (start=" + str(start) + "): " + str(e))
            break
        if not items:
            break
        all_items.extend(items)
        if len(items) < page_display:
            break  # 더 이상 결과가 없음
        start += page_display
        time.sleep(0.12)

    print("  [Naver/전체기간] " + company + " + " + keyword + " -> " + str(len(all_items)) + "건")
    return all_items


def collect_news_by_competitor():
    """반환: {회사명: {"POSITIVE": [...], "NEGATIVE": [...]}}
    각 기사 dict에는 related_count(동일 주제로 보도된 유사 기사 누적 건수)가 포함됩니다.
    """
    result          = {c: {"POSITIVE": [], "NEGATIVE": []} for c in COMPETITORS}
    seen_links      = set()
    accepted_index  = []  # [{"norm": 정규화제목, "article": article_dict}, ...] 전체 수집 기사 (중복/누적집계용)

    for company in COMPETITORS:
        print("\n[검색] " + company)
        company_seen = set()

        for keyword in ESG_KEYWORDS:
            items = search_news(company, keyword)
            time.sleep(0.12)

            for item in items:
                link     = item.get("link", "")
                pub_date = parse_pub_date(item.get("pubDate", ""))

                if not is_recent(pub_date):
                    continue
                if link in seen_links or link in company_seen:
                    continue

                title       = clean_html(item.get("title", ""))
                description = clean_html(item.get("description", ""))
                combined    = title + description

                # 1차: 제목 패턴 필터
                if is_excluded_by_title(title):
                    company_seen.add(link)
                    seen_links.add(link)
                    continue

                # 2차: 제목에 경쟁사명 포함 여부
                if not title_has_competitor(title):
                    print("    [제목 경쟁사 없음] 제외 -> " + title[:50])
                    company_seen.add(link)
                    seen_links.add(link)
                    continue

                # 3차: ESG 키워드 포함 여부
                if not any(kw in combined for kw in ESG_KEYWORDS):
                    continue

                # 4차: 유사 기사 중복 제거 (+ 동일 주제 보도 건수 누적)
                dup_article = find_duplicate_article(title, accepted_index)
                if dup_article is not None:
                    dup_article["related_count"] = dup_article.get("related_count", 1) + 1
                    print(
                        "    [유사 기사] 동일 주제 보도 누적 -> " + title[:50]
                        + " (관련 보도 " + str(dup_article["related_count"]) + "건)"
                    )
                    company_seen.add(link)
                    seen_links.add(link)
                    continue

                # 5차: Gemini 판별 (관련성 + 긍정/부정)
                sentiment = analyze_article(title, description, link)
                company_seen.add(link)
                seen_links.add(link)

                if sentiment is None:
                    continue

                article = {
                    "title":         title,
                    "description":  description,
                    "link":         link,
                    "pub_date":     pub_date,
                    "related_count": 1,
                }
                result[company][sentiment].append(article)
                accepted_index.append({"norm": normalize_title(title), "article": article})
                time.sleep(0.1)

        for senti in ("POSITIVE", "NEGATIVE"):
            result[company][senti].sort(key=lambda x: x["pub_date"], reverse=True)
            result[company][senti] = result[company][senti][:10]

        pos_n = len(result[company]["POSITIVE"])
        neg_n = len(result[company]["NEGATIVE"])
        print("[완료] " + company + ": 긍정 " + str(pos_n) + "건 / 부정 " + str(neg_n) + "건")

    return result


def format_date(pub_date):
    return pub_date.strftime("%Y-%m-%d")


def screening_period_label():
    if SCREENING_MODE == "full":
        return "전체 기간 (Naver 뉴스 검색 API 제공 범위, 쿼리당 최대 " + str(NAVER_MAX_START) + "건)"
    return str(YESTERDAY) + " ~ " + str(TODAY) + " (최근 " + str(LOOKBACK_DAYS) + "일)"


def build_section_plain(news_map, sentiment, section_title):
    lines = [
        "",
        "■■■ " + section_title + " ■■■",
        "",
    ]
    has_any = False
    for company in COMPETITORS:
        articles = news_map.get(company, {}).get(sentiment, [])
        if not articles:
            continue
        has_any = True
        lines.append("▶ " + company + "  (" + str(len(articles)) + "건)")
        lines.append("-" * 50)
        for art in articles:
            date_str = format_date(art["pub_date"])
            lines.append("[" + date_str + "] " + art["title"])
            if art["description"]:
                desc = art["description"][:120]
                if len(art["description"]) > 120:
                    desc += "..."
                lines.append("  " + desc)
            lines.append("  링크: " + art["link"])
            lines.append("")
        lines.append("")

    if not has_any:
        lines.append("※ 해당 기간 " + section_title + " 뉴스가 없습니다.")
        lines.append("")
    return lines


def build_plain_report(news_map):
    today_str = TODAY.strftime("%Y년 %m월 %d일")
    lines = [
        "=" * 60,
        "  유통업계 ESG 동향 리포트  |  " + today_str,
        "=" * 60,
        "검색 기간: " + screening_period_label(),
    ]

    lines += build_section_plain(news_map, "POSITIVE", "긍정 동향")
    lines += build_section_plain(news_map, "NEGATIVE", "부정 동향")

    lines += [
        "=" * 60,
        "본 리포트는 GS리테일 ESG파트 자동화 시스템에서 발송됩니다.",
        "=" * 60,
    ]
    return "\n".join(lines)


def build_section_html(news_map, sentiment, section_title, header_color):
    rows = '<div class="section-title" style="color:' + header_color + '; border-color:' + header_color + ';">' + section_title + '</div>'
    has_any = False

    for company in COMPETITORS:
        articles = news_map.get(company, {}).get(sentiment, [])
        if not articles:
            continue
        has_any = True

        article_html = ""
        for art in articles:
            date_str = format_date(art["pub_date"])
            desc = art["description"][:150]
            if len(art["description"]) > 150:
                desc += "..."
            article_html += '<div class="article">'
            article_html += '<span class="date">[' + date_str + ']</span> '
            article_html += '<a class="title" href="' + art["link"] + '" target="_blank">' + art["title"] + '</a>'
            article_html += '<p class="desc">' + desc + '</p>'
            article_html += '</div>'

        rows += '<div class="company-block">'
        rows += '<div class="company-header">' + company + ' <span class="count">' + str(len(articles)) + '건</span></div>'
        rows += article_html
        rows += '</div>'

    if not has_any:
        rows += '<p class="no-news">※ 해당 기간 ' + section_title + ' 뉴스가 없습니다.</p>'
    return rows


def build_html_report(news_map):
    today_str  = TODAY.strftime("%Y년 %m월 %d일")
    period_str = screening_period_label()

    rows  = build_section_html(news_map, "POSITIVE", "🟢 긍정 동향", "#1A7F37")
    rows += build_section_html(news_map, "NEGATIVE", "🔴 부정 동향", "#C0392B")

    css = """
  body { font-family: 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif;
         background:#f5f7fa; margin:0; padding:20px; color:#222; }
  .wrapper { max-width:700px; margin:auto; background:#fff;
             border-radius:10px; overflow:hidden;
             box-shadow:0 2px 12px rgba(0,0,0,.1); }
  .header { background:#005BAC; color:#fff; padding:28px 32px; }
  .header h1 { margin:0; font-size:20px; }
  .header p  { margin:6px 0 0; font-size:13px; opacity:.85; }
  .body { padding:24px 32px; }
  .section-title { font-size:17px; font-weight:800; margin:24px 0 16px;
                   padding-bottom:8px; border-bottom:2px solid; }
  .company-block { margin-bottom:28px; border:1px solid #e8edf2;
                   border-radius:8px; overflow:hidden; }
  .company-header { background:#EBF2FB; padding:10px 16px;
                    font-weight:700; font-size:15px; color:#005BAC; }
  .count { font-size:12px; font-weight:400; color:#666; margin-left:6px; }
  .article { padding:12px 16px; border-top:1px solid #f0f0f0; }
  .date { font-size:12px; color:#888; margin-right:6px; }
  .title { font-size:14px; font-weight:600; color:#1a1a1a;
           text-decoration:none; display:inline; }
  .title:hover { color:#005BAC; text-decoration:underline; }
  .desc { margin:4px 0 0; font-size:12px; color:#555; line-height:1.6; }
  .no-news { color:#888; text-align:center; padding:24px; }
  .footer { background:#f5f7fa; padding:16px 32px;
            font-size:11px; color:#999; text-align:center;
            border-top:1px solid #e8edf2; }
"""

    html  = "<!DOCTYPE html>\n"
    html += '<html lang="ko">\n'
    html += "<head>\n<meta charset=\"UTF-8\">\n<style>\n" + css + "\n</style>\n</head>\n"
    html += "<body>\n"
    html += '<div class="wrapper">\n'
    html += '  <div class="header">\n'
    html += "    <h1>유통업계 ESG 동향 리포트</h1>\n"
    html += "    <p>기준일: " + today_str + " &nbsp;|&nbsp; 검색기간: " + period_str + "</p>\n"
    html += "  </div>\n"
    html += '  <div class="body">' + rows + "</div>\n"
    html += '  <div class="footer">본 리포트는 GS리테일 ESG파트 자동화 시스템에서 자동 발송됩니다.</div>\n'
    html += "</div>\n</body>\n</html>"
    return html


def send_email(plain_body, html_body):
    today_str = TODAY.strftime("%Y-%m-%d")
    subject   = "[ESG 동향] " + today_str + " 유통업계 ESG 리포트"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = ", ".join(RECIPIENTS)

    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body,  "html",  "utf-8"))

    print("[이메일] SMTP 연결 중...")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        print("[이메일] 로그인 성공! 발송 중...")
        smtp.sendmail(GMAIL_USER, RECIPIENTS, msg.as_bytes())

    print("[완료] 이메일 발송 -> " + ", ".join(RECIPIENTS))


# ----------------------------------------------------------------------------
# 대시보드(정적 JSON) 데이터 출력
#
# docs/data/<YYYY-MM>.json   : 해당 월에 수집된 기사 전체 (회사·감성·관련보도건수 포함)
# docs/data/manifest.json    : 존재하는 월 목록 + 마지막 갱신 시각 (대시보드가 가장 먼저 읽는 파일)
#
# 매 실행마다 해당 월 파일을 불러와 link 기준으로 중복 없이 병합합니다.
# (YESTERDAY~TODAY 검색 기간이 겹치므로 같은 기사가 여러 번 수집될 수 있음)
# ----------------------------------------------------------------------------

def export_dashboard_data(news_map, output_dir=None):
    output_dir = output_dir or DASHBOARD_DATA_DIR
    os.makedirs(output_dir, exist_ok=True)

    # 기사를 실제 보도일(pub_date) 기준 월별로 분배 (수집일이 아니라 보도일 기준이어야
    # "일자별 리포트"·"월별 모음" 화면에서 날짜가 어긋나지 않음)
    by_month = {}
    for company in COMPETITORS:
        for sentiment in ("POSITIVE", "NEGATIVE"):
            for art in news_map[company][sentiment]:
                month_key = art["pub_date"].strftime("%Y-%m")
                by_month.setdefault(month_key, []).append({
                    "date":          format_date(art["pub_date"]),
                    "company":       company,
                    "sentiment":     sentiment,
                    "title":         art["title"],
                    "description":  art["description"],
                    "link":         art["link"],
                    "related_count": art.get("related_count", 1),
                })

    touched_months = set()

    for month_key, new_records in by_month.items():
        month_file = os.path.join(output_dir, month_key + ".json")

        if os.path.exists(month_file):
            with open(month_file, "r", encoding="utf-8") as f:
                existing = json.load(f)
        else:
            existing = []

        existing_by_link = {a["link"]: a for a in existing}
        added, updated = 0, 0

        for rec in new_records:
            prev = existing_by_link.get(rec["link"])
            if prev is None:
                existing.append(rec)
                existing_by_link[rec["link"]] = rec
                added += 1
            elif prev.get("related_count", 1) != rec["related_count"]:
                # 이미 저장된 기사인데 이번 실행에서 관련 보도 건수가 더 늘어난 경우 갱신
                prev["related_count"] = rec["related_count"]
                updated += 1

        with open(month_file, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)

        touched_months.add(month_key)
        print("[대시보드] " + month_file + " 갱신 (신규 " + str(added) + "건 / 갱신 " + str(updated) + "건)")

    # manifest.json 갱신 (대시보드가 어떤 월 파일들이 존재하는지 알기 위함)
    manifest_file = os.path.join(output_dir, "manifest.json")
    months = set(touched_months)
    for fname in os.listdir(output_dir):
        if re.match(r"^\d{4}-\d{2}\.json$", fname):
            months.add(fname.replace(".json", ""))

    manifest = {
        "months":       sorted(months),
        "last_updated": datetime.now(KST).isoformat(),
    }
    with open(manifest_file, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print("[대시보드] " + manifest_file + " 갱신 완료 (" + str(len(months)) + "개월 분)")


def main():
    print("[시작] " + datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S") + " ESG 리포트 생성")
    news_map   = collect_news_by_competitor()
    plain_body = build_plain_report(news_map)
    html_body  = build_html_report(news_map)
    print("\n[리포트 미리보기]\n" + plain_body[:500])
    send_email(plain_body, html_body)
    export_dashboard_data(news_map)


if __name__ == "__main__":
    main()
