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

# ESG 검색 키워드
# 축소 전 17개 → 8개로 축소. 무료 티어 Gemini 판별 호출 수를 절반 이하로 줄이는 게 목적.
# 제거한 키워드와 그 이유:
#   - "지속가능성"/"지속가능경영" → "ESG"에 포함
#   - "기후변화"/"기후대응" → "탄소중립"에 포함
#   - "탄소절감"/"탄소저감"/"탄소배출" → "탄소중립"에 포함
#   - "CSR" → "사회공헌"에 포함 (국내 매체 대다수가 사회공헌으로 표기)
#   - "ISO" → 유통업 특성상 ESG 문맥에서 등장 빈도 낮음
#   - "사회적책임" → "사회공헌"과 사실상 동의어
ESG_KEYWORDS = [
    "ESG", "탄소중립", "사회공헌", "상생",
    "동반성장", "지배구조", "기부", "지속가능경영",
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
    r"유통딜리버리",
    r"유통 딜리버리",
    r"주간 유통",
    r"주간유통",
    r"유통 트렌드",
    r"유통트렌드",
    r"유통 브리핑",
    r"유통브리핑",
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


# 유통사 나열형 종합 기사 감지에 사용하는 확장 리스트.
# COMPETITOR_NAMES에 없는 유통 관련 회사 이름들도 포함해서,
# 여러 회사가 나열된 브리핑성 기사를 잡아냄.
RETAIL_ORG_NAMES_FOR_COUNTING = COMPETITOR_NAMES + [
    "GS25", "CU", "SSG닷컴", "쿠팡", "홈플러스", "롯데쇼핑",
    "신세계", "홈앤쇼핑", "위메프", "티몬", "무신사", "올리브영",
    "29CM", "전자랜드", "CJ대한통운", "배민", "요기요",
    "다이소", "농협하나로마트", "롯데온",
]


def count_retailers_in_title(title):
    """제목에 등장하는 유통 관련 회사 이름 개수를 반환.
    긴 이름을 우선적으로 매칭해 부분 문자열 중복 카운트를 방지한다.
    (예: '이마트24'가 들어있으면 '이마트'는 별개로 세지 않음)
    """
    remaining = title
    count = 0
    # 긴 이름부터 매칭하여 겹치는 부분 문자열 중복 카운트 방지
    for name in sorted(set(RETAIL_ORG_NAMES_FOR_COUNTING), key=len, reverse=True):
        while name in remaining:
            count += 1
            remaining = remaining.replace(name, " ", 1)
    return count


def is_multi_retailer_briefing(title, threshold=3):
    """제목에 유통사가 threshold 개 이상 나열되어 있으면 True.
    '[유통딜리버리] 신세계百·이마트·BGF리테일·세븐일레븐·GS25·...' 같은
    브리핑성 종합 기사를 자동으로 걸러내기 위한 근본 방어책이다.
    """
    n = count_retailers_in_title(title)
    return n >= threshold


def title_has_competitor(title):
    for name in COMPETITOR_NAMES:
        if name in title:
            return True
    return False


def normalize_title(title):
    """유사도 비교를 위한 제목 정규화.
    - 대괄호 머리말 [단독], [속보], [유통], [ESG] 등 제거
    - 언론 관용어 '···', '…' 등 축약 표시 제거
    - 특수문자·공백 제거
    """
    t = re.sub(r"\[.*?\]", "", title)             # 대괄호 머리말 제거
    t = re.sub(r"['\"·…‥]+", "", t)               # 관용 부호 제거
    t = re.sub(r"[^\w가-힣]", "", t)               # 특수문자·공백 제거
    return t.lower()


def find_duplicate_article(title, accepted_index, threshold=0.45):
    """이미 수집된 기사 중 유사 기사를 찾아 그 기사 dict를 반환 (없으면 None).
    동일 주제로 여러 매체가 보도한 경우 원본 기사의 related_count를 누적시키기 위해 사용.

    threshold를 0.6 → 0.45로 낮춰 같은 주제의 다른 매체 기사도 잘 잡히도록 함.
    (예: '롯데마트·슈퍼 영월 농촌 체험' vs '롯데마트·슈퍼, 임직원 가족과 농촌 체험'
         → 이전 임계값에선 놓쳤으나 Gemini 호출 낭비의 주범이었음)
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


# ----------------------------------------------------------------------------
# Gemini 기반 기사 분석
#
# 설계 원칙:
# 1) 판별 품질 유지 + rate limit 안정성 확보
#    - 관련성/감성을 한 번의 호출로 판별하되, 프롬프트에 두 판별을 명시적으로
#      분리해서 요청 → 품질 유지하면서 호출 수는 기사당 1회
# 2) 표면 매칭 방지: "준법경영", "동반성장", "컴플라이언스", "최우수", "지속가능"
#    같은 단어는 부정 기사에도 자주 등장. 단어 매칭이 아니라 "회사에 무엇이
#    일어났는지"를 기준으로 판별하도록 유도.
# 3) 근거 요구: JSON 형식으로 label + reason + key_phrases를 받아 로그에 남김.
#    오분류가 발생하면 로그에서 판단 근거를 바로 확인할 수 있음.
# 4) 429 (rate limit) 안전 처리:
#    - 지수적 백오프로 자동 재시도 (5s → 15s → 45s)
#    - 호출 사이 최소 간격 확보 (전역 스로틀)
#    - 재시도 후에도 실패하면 UNCERTAIN 으로 저장 (기존엔 아예 제외 → 빈 리포트)
# ----------------------------------------------------------------------------

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
GEMINI_URL   = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    + GEMINI_MODEL + ":generateContent?key=" + GEMINI_API_KEY
)

# 무료 티어 Gemini 2.5 Flash-Lite: 15 RPM (분당 15회) → 안전하게 4초 간격으로 스로틀
GEMINI_MIN_INTERVAL = float(os.environ.get("GEMINI_MIN_INTERVAL", "4.0"))
GEMINI_MAX_RETRIES  = 3
_last_gemini_call_ts = 0.0   # 마지막 호출 시각 (전역 스로틀용)


def _extract_json_block(text):
    """Gemini 응답에서 JSON 객체만 뽑아 dict로 반환. 실패 시 None."""
    if not text:
        return None
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _throttle_gemini():
    """직전 호출에서 GEMINI_MIN_INTERVAL 초 이상 경과할 때까지 대기."""
    global _last_gemini_call_ts
    now = time.time()
    elapsed = now - _last_gemini_call_ts
    if elapsed < GEMINI_MIN_INTERVAL:
        wait = GEMINI_MIN_INTERVAL - elapsed
        time.sleep(wait)
    _last_gemini_call_ts = time.time()


def _call_gemini(prompt, timeout=30):
    """Gemini 호출. 429/5xx는 지수적 백오프로 재시도. 성공 시 응답 텍스트, 최종 실패 시 None."""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.0,
            "responseMimeType": "application/json",
        },
    }

    backoff = 5   # 초, 429 발생 시 대기 시간의 시작점
    for attempt in range(GEMINI_MAX_RETRIES + 1):
        _throttle_gemini()
        try:
            r = requests.post(GEMINI_URL, json=payload, timeout=timeout)

            # 429 (rate limit) 또는 5xx (서버 오류) → 재시도
            if r.status_code == 429 or 500 <= r.status_code < 600:
                if attempt < GEMINI_MAX_RETRIES:
                    print(f"    [Gemini {r.status_code}] rate limit/서버오류, {backoff}초 대기 후 재시도 ({attempt+1}/{GEMINI_MAX_RETRIES})")
                    time.sleep(backoff)
                    backoff *= 3   # 5 → 15 → 45
                    continue
                else:
                    print(f"    [Gemini {r.status_code}] 최대 재시도 초과, 포기")
                    return None

            r.raise_for_status()
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]

        except requests.exceptions.RequestException as e:
            if attempt < GEMINI_MAX_RETRIES:
                print(f"    [Gemini 오류] {str(e)[:80]}, {backoff}초 대기 후 재시도")
                time.sleep(backoff)
                backoff *= 3
                continue
            print(f"    [Gemini 최종 실패] {str(e)[:80]}")
            return None
        except Exception as e:
            print(f"    [Gemini 파싱 오류] {str(e)[:80]}")
            return None
    return None


def _build_analysis_prompt(title, content):
    """관련성 판별 + 감성 판별을 한 번의 호출로 요청하는 프롬프트."""
    return "\n".join([
        "당신은 유통업계 ESG 뉴스 클리핑 담당자입니다.",
        "아래 기사에 대해 두 가지를 판별하세요:",
        "  (1) 이 기사가 '경쟁 유통사 ESG 동향 모니터링 대시보드'에 실을 만한 기사인가?",
        "  (2) 실을 만하다면, 해당 회사에 유리한 소식인가, 불리한 소식인가?",
        "",
        "===== (1) 관련성 판별 규칙 =====",
        "",
        "## 규칙 1-A: 핵심 주체",
        "기사의 '주인공'이 다음 회사 중 하나여야 합니다:",
        "- GS리테일, GS25, GS더프레시",
        "- BGF리테일, CU",
        "- 세븐일레븐, 코리아세븐",
        "- 이마트, 이마트24, 이마트에브리데이",
        "- 롯데쇼핑, 롯데슈퍼, 롯데마트, 롯데홈쇼핑, 롯데백화점",
        "- 현대백화점, 현대홈쇼핑",
        "- CJ온스타일",
        "- 신세계백화점",
        "",
        "관련 없음(include=false) 예시:",
        "- 위 회사가 판매채널·입점처로만 언급됨 (예: '이마트에서 판매하는 A제품이 인기')",
        "- 위 회사가 시장 배경/비교 대상으로만 등장",
        "- 여러 회사를 나열한 요약·종합 기사",
        "- 증권사 리포트, 목표주가, 공시, 지분 매매 기사",
        "- 부동산·정치·선거·연예·스포츠 등 유통과 무관한 기사",
        "",
        "## 규칙 1-B: ESG 관련성",
        "기사가 회사의 다음 ESG 이슈 중 하나 이상을 실질적으로 다뤄야 합니다 (긍정/부정 무관):",
        "- E(환경): 탄소중립, 재생에너지, 친환경 포장·물류, 폐기물, 환경 인증/규제/위반",
        "- S(사회): 사회공헌, 상생/동반성장, 협력사·가맹점 관계(갑질·불공정 포함), 소비자 이슈, 노사·안전사고, 공정거래 위반, 하도급법 위반, 담합, 과징금, 리콜, 불매",
        "- G(지배구조): 이사회 개편, ESG위원회, 지속가능경영보고서, 오너 리스크, 준법경영 위반, 내부통제 실패, 횡령·배임, 감독기관 제재, 소송",
        "",
        "중요: 단순히 'ESG', '준법경영', '지속가능', '동반성장' 단어가 등장한다고 관련 기사가 아닙니다.",
        "회사가 실제로 그 이슈에서 무엇을 했는지(또는 회사에 무엇이 일어났는지)가 기사의 중심 소재여야 합니다.",
        "",
        "## 규칙 1-C: 스코프",
        "회사의 해외 법인·해외 시장 활동만 다루는 기사는 제외 (국내 이슈 포함이면 OK).",
        "",
        "## 규칙 1-D: 기사 형태",
        "단일 주제를 깊이 다루는 실제 뉴스 기사여야 합니다. 여러 단신 나열/광고성 보도자료는 제외.",
        "",
        "===== (2) 감성 판별 규칙 (관련 기사에 한해) =====",
        "",
        "## 가장 중요한 원칙: 방향성 판별 (반드시 먼저 확인)",
        "",
        "부정적으로 보이는 단어(사죄·반성·절도·피해·사고·논란 등)가 등장했을 때,",
        "그 단어의 주체와 대상이 무엇인지를 반드시 먼저 파악하세요.",
        "",
        "구조 A) 회사가 잘못을 저지르거나 회사에 나쁜 일이 일어남 → NEGATIVE",
        "  예: 회사가 소비자를 속임, 회사가 제재를 받음, 회사 제품에 사고 발생",
        "  예: '이마트, 제품 결함으로 리콜' → NEGATIVE",
        "",
        "구조 B) 외부에서 발생한 사건을 회사가 사회공헌 등으로 승화 → POSITIVE",
        "  예: 소비자가 과거 잘못을 뉘우치며 회사에 보상금을 보냈고, 회사가 이를 기부로 환원",
        "  예: 지역사회의 어려움을 회사가 후원으로 해결",
        "  예: '이마트, 소비자 사죄 편지·보상금 기부단체에 전달' → POSITIVE (미담)",
        "",
        "구조 C) 회사가 스스로 반성·사과 → 사안에 따라 판단",
        "  회사가 자기 잘못을 인정하며 사과문을 냈다면 NEGATIVE (문제 발생이 원인)",
        "  단, 회사가 이를 계기로 개선 프로그램을 발표한 것이 기사 중심이면 POSITIVE 가능",
        "",
        "핵심: '누가 누구에게 무엇을 했는가'의 화살표 방향이 회사→피해자 인지, 외부→회사 인지",
        "반드시 구분하세요. 단어만 보고 판단하지 마세요.",
        "",
        "## NEGATIVE (불리) — 아래 신호가 하나라도 명확하고 회사가 유발/책임 주체이면 NEGATIVE:",
        "- 감독기관 제재·조사·과징금·고발 (공정위·금감원·국세청·검찰·경찰)",
        "- 유죄 판결, 소송, 법원 판결, 벌금",
        "- 갑질, 불공정 거래, 하도급 위반, 담합, 부당한 판촉비 전가",
        "- 횡령, 배임, 내부통제 실패, 은폐, 회계 부정",
        "- 협력사·가맹점주 반발, 노조 요구, 파업",
        "- 회사가 원인이 된 소비자 피해, 리콜, 불매, 안전사고, 사망 사고",
        "- 오너 리스크, CEO 리더십 비판, 준법경영 실패",
        "- 부정 등급/평가, 인증 취소, 상장폐지 우려",
        "- 기사 논조가 회사에 대한 문제 지적·의혹 제기·비판적 분석",
        "",
        "## POSITIVE (유리) — 아래 신호가 명확하면 POSITIVE:",
        "- 수상, 인증 획득, 등급 상향",
        "- ESG 활동·투자·프로그램 신규 시작 또는 확대",
        "- 협약, MOU, 파트너십 체결",
        "- 실질적 성과 발표 (탄소배출량 감축 달성 등)",
        "- 지속가능경영보고서 발간, 위원회 신설 등 지배구조 개선",
        "- 기부, 후원, 취약계층 지원, 상생 프로그램 운영",
        "- 외부 사건을 사회공헌·기부·상생 활동으로 승화한 미담",
        "- 기사 논조가 성과 소개·모범 사례·미담으로 우호적",
        "",
        "## NEUTRAL — 위 어디에도 명확히 해당하지 않는 사실 전달 기사 (저장하지 않음)",
        "",
        "## 감성 판별 주의사항 (매우 중요)",
        "1. 표면 단어에 속지 마세요. '준법경영', '동반성장', '컴플라이언스', '최우수', '지속가능'이 나와도 그 단어가 등장한 맥락을 봐야 합니다.",
        "   예: '준법경영을 강조했지만 실제로는 위반 사례가 잇따르고 있다' → NEGATIVE",
        "   예: '최우수 등급을 유지해왔는데 이번 판결로 등급 추락 가능성이 있다' → NEGATIVE",
        "2. 반대로, 부정적 단어가 있어도 회사가 그 대상이 아닌 경우에 주의하세요.",
        "   예: '절도·사죄·반성' 같은 단어가 있어도, 회사는 오히려 그것을 사회공헌으로 승화한 주체 → POSITIVE",
        "   예: '피해자 돕는 이마트' → POSITIVE (회사가 피해자를 지원한 것)",
        "3. 제목 표현: '[단독]', '바람 잘 날 없는', '영이 안 선다', '허점', '흔들', '논란', '의혹' → 부정 신호",
        "4. 좋은/나쁜 소식이 섞여 있으면, 기사의 중심 메시지 기준으로 판단",
        "5. 회사 측의 해명·반론이 인용되어도 기사 전체가 회사에 대한 문제 제기 성격이면 NEGATIVE",
        "",
        "===== 출력 형식 =====",
        "다음 JSON만 반환하세요. 다른 텍스트는 절대 포함하지 마세요.",
        '{',
        '  "include": true|false,',
        '  "subject_company": "위 회사 목록 중 정확한 이름 하나 (해당 없으면 null)",',
        '  "esg_dimensions": ["E"/"S"/"G" 중 해당하는 것들, 없으면 []],',
        '  "scope": "domestic" | "overseas" | "mixed" | "unknown",',
        '  "direction": "회사가 잘못/문제를 유발" | "회사에 나쁜 일이 발생" | "회사가 좋은 일을 함" | "외부 사건을 회사가 미담으로 승화" | "해당 없음",',
        '  "label": "POSITIVE" | "NEGATIVE" | "NEUTRAL" | "N/A",',
        '  "reason": "판단 근거를 한 문장으로 (특히 방향성 판단을 명시)",',
        '  "key_phrases": ["판단 근거가 된 기사 원문 표현 2~4개"]',
        '}',
        "설명:",
        "- include=false 인 경우 label과 direction은 각각 \"N/A\", \"해당 없음\"으로 두세요.",
        "- include=true 인 경우 direction과 label을 반드시 채우세요.",
        "",
        "===== 판별할 기사 =====",
        "제목: " + (title or ""),
        "본문: " + (content or ""),
    ])


def analyze_article(title, description, article_url):
    """관련성 + 감성을 한 번의 Gemini 호출로 판별.
    반환: 'POSITIVE' / 'NEGATIVE' / 'UNCERTAIN' / None(제외)
      - None      : 대상 아님 또는 중립 기사
      - POSITIVE  : 대상이고, 회사에 유리한 소식
      - NEGATIVE  : 대상이고, 회사에 불리한 소식
      - UNCERTAIN : Gemini 호출 자체가 실패해 판별 불가 (수동 검토 대상, 대시보드에는 표시됨)
    """
    body_text = fetch_article_body(article_url)

    if body_text and is_paywalled(body_text):
        print("    [유료 기사] 제외 -> " + title[:40])
        return None

    content = body_text if body_text else description
    if not body_text:
        print("    [본문 없음] description으로 대체 판단 -> " + title[:40])

    prompt = _build_analysis_prompt(title, content)
    raw = _call_gemini(prompt)

    if raw is None:
        # 재시도까지 모두 실패했을 때만 이 분기에 도달.
        # 여기서 그냥 None(제외)로 처리하면 rate limit 발생 순간부터 리포트가 텅 비어버림.
        # → UNCERTAIN으로 저장해 대시보드에서 수동 검토할 수 있게 함.
        print("    [Gemini 판별 실패] UNCERTAIN 으로 저장 -> " + title[:40])
        return "UNCERTAIN"

    data = _extract_json_block(raw)
    if not isinstance(data, dict):
        print("    [Gemini JSON 파싱 실패] UNCERTAIN 으로 저장 -> " + title[:40])
        return "UNCERTAIN"

    if not data.get("include"):
        reason = str(data.get("reason", ""))[:80]
        print("    [관련성 없음] " + reason + " -> " + title[:40])
        return None

    label = str(data.get("label", "")).upper()
    reason = str(data.get("reason", ""))[:100]
    direction = str(data.get("direction", ""))[:40]
    phrases = data.get("key_phrases") or []
    phrase_str = " / ".join(str(p) for p in phrases[:3])
    dir_suffix = " | 방향: " + direction if direction and direction != "해당 없음" else ""

    if label == "POSITIVE":
        print("    [Gemini] 긍정 -> " + title[:40] + " | 근거: " + reason + dir_suffix)
        return "POSITIVE"
    if label == "NEGATIVE":
        print("    [Gemini] 부정 -> " + title[:40] + " | 근거: " + reason + dir_suffix + " | 표현: " + phrase_str)
        return "NEGATIVE"
    if label == "NEUTRAL":
        print("    [Gemini] 중립 제외 -> " + title[:40] + " | 근거: " + reason)
        return None

    # 라벨 불명 (include=true인데 label이 이상한 경우)
    print("    [Gemini] 라벨 불명(" + label + ") UNCERTAIN -> " + title[:40])
    return "UNCERTAIN"


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
    """반환: {회사명: {"POSITIVE": [...], "NEGATIVE": [...], "UNCERTAIN": [...]}}
    각 기사 dict에는 related_count(동일 주제로 보도된 유사 기사 누적 건수)가 포함됩니다.
    UNCERTAIN 은 Gemini 감성 판별이 실패한 기사로, 나중에 수동 재검토 대상입니다.
    """
    result          = {c: {"POSITIVE": [], "NEGATIVE": [], "UNCERTAIN": []} for c in COMPETITORS}
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

                # 1.5차: 유통사 3개 이상 나열 (종합·브리핑 기사) 자동 제외
                if is_multi_retailer_briefing(title):
                    print("    [종합 기사] 유통사 " + str(count_retailers_in_title(title)) + "개 나열 제외 -> " + title[:50])
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

        for senti in ("POSITIVE", "NEGATIVE", "UNCERTAIN"):
            result[company][senti].sort(key=lambda x: x["pub_date"], reverse=True)
            result[company][senti] = result[company][senti][:10]

        pos_n = len(result[company]["POSITIVE"])
        neg_n = len(result[company]["NEGATIVE"])
        unc_n = len(result[company]["UNCERTAIN"])
        print("[완료] " + company + ": 긍정 " + str(pos_n) + "건 / 부정 " + str(neg_n) + "건 / 검토필요 " + str(unc_n) + "건")

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
        for sentiment in ("POSITIVE", "NEGATIVE", "UNCERTAIN"):
            for art in news_map[company].get(sentiment, []):
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
