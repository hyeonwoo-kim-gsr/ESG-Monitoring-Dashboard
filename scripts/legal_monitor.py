#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
legal_monitor.py - ESG 법률 모니터링 (의안정보시스템 + 국가법령정보센터)

* 의안  : 열린국회정보 Open API(TVBPMBILL11 목록, BILLINFODETAIL 일정) + 의안정보시스템(likms)의
          '제안이유 및 주요내용'(세션+CSRF 방식으로 billInfo 탭 조회)
* 법령  : 국가법령정보센터 Open API(lawSearch / lawService)로 공포일 기준 목록·제개정이유 수집
* 분류  : Gemini 2단계
            1) 이름만 보고 후보 선별(관대한 기준, 호출 1회에 100건)
            2) 본문(제안이유/제개정이유)을 보고 ENV_REG / DISC_MANDATE / SOC_CONTRIB / IRRELEVANT 확정
               + 제·개정 배경, 주요 내용 요약 생성. 근거 인용문이 원문에서 확인되지 않으면 IRRELEVANT.
* 저장  : docs/legal/bills.json, laws.json, meta.json, state.json  (원문 전체는 저장하지 않고 요약만 저장)
* 최근 2개년(기본 730일) 먼저 적재, 이후 매 실행마다 신규/변경분만 Gemini 호출. 호출 상한에 걸려도 다음 실행에서 이어감.

필요 환경변수
  ASSEMBLY_API_KEY  열린국회정보 인증키 (open.assembly.go.kr, 즉시 발급)
  LAW_OC            국가법령정보센터 Open API 인증값(OC, 신청 시 사용한 이메일 ID)
  GEMINI_API_KEY    (선택: GEMINI_MODEL, GEMINI_MIN_INTERVAL)
  LAW_REFERER       (선택) 법제처에 등록한 도메인. 지정하면 Referer 헤더로 전송

사용 예
  python scripts/legal_monitor.py --diagnose          # Gemini 호출 없이 API 연결/필드/본문 추출 점검
  python scripts/legal_monitor.py                      # 초기 적재 + 일일 갱신(동일 명령)
  python scripts/legal_monitor.py --only bills --max-calls 40
이메일은 발송하지 않는다.
"""
import os
import re
import sys
import json
import html as htmllib
import time
import argparse
import datetime as dt

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import esg_classifier as clf  # noqa: E402  (parse_response / evidence_in_text / make_gemini_caller 재사용)

ASSEMBLY_BASE = "https://open.assembly.go.kr/portal/openapi/"
BILL_SERVICE = os.environ.get("ASSEMBLY_BILL_SERVICE", "TVBPMBILL11")  # 의안정보(통합) 서비스명
LAW_SEARCH = "https://www.law.go.kr/DRF/lawSearch.do"
LAW_SERVICE = "https://www.law.go.kr/DRF/lawService.do"
LAW_BODY_URL = "https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq=%s"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept-Language": "ko-KR,ko;q=0.9"}

CATEGORIES = ("ENV_REG", "DISC_MANDATE", "SOC_CONTRIB")
CATEGORY_LABEL = {"ENV_REG": "환경규제", "DISC_MANDATE": "의무공시", "SOC_CONTRIB": "사회공헌"}

TRIAGE_BATCH = 100   # 이름 선별은 1회 호출에 100건 (22대 법률안이 많아 호출 수 절감)
CLASSIFY_BATCH = 4
TEXT_LIMIT = 3000

# ============================================================================ 공통 유틸
KST = dt.timezone(dt.timedelta(hours=9))


def today_kst():
    return dt.datetime.now(KST).date()


def ymd(s):
    """'20241124' / '2024-11-24' / '2024.11.24' -> '2024-11-24' (실패 시 '')"""
    if not s:
        return ""
    m = re.search(r"(\d{4})\D?(\d{2})\D?(\d{2})", str(s))
    return "%s-%s-%s" % m.groups() if m else ""


def g(row, *names, default=""):
    for n in names:
        v = row.get(n)
        if v not in (None, ""):
            return v
    return default


def strip_tags(s):
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = htmllib.unescape(s)
    s = re.sub(r"[ \t\u00a0\u3000]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def flatten_text(obj, out=None):
    """중첩 dict/list 안의 문자열을 모두 모은다 (법령 JSON 본문용)."""
    if out is None:
        out = []
    if isinstance(obj, str):
        t = obj.strip()
        if t:
            out.append(t)
    elif isinstance(obj, list):
        for x in obj:
            flatten_text(x, out)
    elif isinstance(obj, dict):
        for v in obj.values():
            flatten_text(v, out)
    return out


def http_get(url, params=None, headers=None, timeout=40, tries=3):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=headers or UA, timeout=timeout)
            if r.status_code >= 500:
                raise RuntimeError("HTTP %d" % r.status_code)
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError("요청 실패: %s (%s)" % (url.split("?")[0], str(last)[:80]))


class SourceError(Exception):
    """한 출처(의안/법령) 전체를 못 쓰는 오류 (키 오류·IP 검증 실패 등)."""


# ============================================================================ 의안: 열린국회정보
def _assembly_rows(data, service):
    """열린국회정보 JSON -> (rows, total, code, message)"""
    if "RESULT" in data and service not in data:  # 오류 응답
        res = data["RESULT"]
        return [], 0, res.get("CODE", ""), res.get("MESSAGE", "")
    blocks = data.get(service) or []
    total, code, msg, rows = 0, "", "", []
    for b in blocks:
        for h in b.get("head", []) or []:
            if "list_total_count" in h:
                total = int(h["list_total_count"])
            if "RESULT" in h:
                code, msg = h["RESULT"].get("CODE", ""), h["RESULT"].get("MESSAGE", "")
        if "row" in b:
            rows = b["row"]
    return rows, total, code, msg


def fetch_bill_rows(key, ages, since, page_size=1000, service=None, max_pages=60):
    """의안 목록(법률안 위주 필터는 호출측). since: 'YYYY-MM-DD' 이후 접수분만."""
    service = service or BILL_SERVICE
    out = []
    for age in ages:
        page = 1
        while page <= max_pages:
            r = http_get(ASSEMBLY_BASE + service,
                         params={"KEY": key, "Type": "json", "pIndex": page, "pSize": page_size, "AGE": age})
            try:
                data = r.json()
            except Exception:
                raise SourceError("의안 API 응답이 JSON이 아닙니다: " + r.text[:100])
            rows, total, code, msg = _assembly_rows(data, service)
            if code.startswith("ERROR"):
                raise SourceError("의안 API 오류 %s: %s" % (code, msg))
            if code == "INFO-200" or not rows:
                break
            out.extend(rows)
            if page * page_size >= total:
                break
            page += 1
    return [r for r in out if ymd(g(r, "PROPOSE_DT")) >= since]


BILL_SUFFIX = re.compile(r"\s*(전부개정|일부개정|폐지|제정)?\s*법률안(\(.*?\))?\s*$")


def is_law_bill(row):
    return "법률안" in g(row, "BILL_NAME") or g(row, "BILL_NAME").endswith("법안")


def bill_target_law(name):
    """'법인세법 일부개정법률안(홍길동의원 등 10인)' -> '법인세법'"""
    n = re.sub(r"\(.*?\)\s*$", "", name or "").strip()
    n = re.sub(r"\s*(전부개정|일부개정|폐지|제정)?\s*법률안\s*$", "", n).strip()
    return n


LIKMS = "https://likms.assembly.go.kr"
BILL_DETAIL_API = "BILLINFODETAIL"   # 의안 상세정보(본회의·정부이송·공포 일자) - 열린국회정보
_LIKMS_UA = {"User-Agent": UA["User-Agent"], "Accept-Language": "ko-KR,ko;q=0.9"}


def fetch_bill_dates(bill_id):
    """열린국회정보 BILLINFODETAIL -> {'transfer','promulgation','promulgation_no','plenary_date','plenary_result'}. 실패 시 None."""
    key = os.environ.get("ASSEMBLY_API_KEY", "")
    if not (key and bill_id):
        return None
    try:
        r = http_get(ASSEMBLY_BASE + BILL_DETAIL_API,
                     params={"KEY": key, "Type": "json", "pIndex": 1, "pSize": 1, "BILL_ID": bill_id},
                     timeout=30, tries=2)
        rows, _t, code, _m = _assembly_rows(r.json(), BILL_DETAIL_API)
    except Exception as e:  # noqa: BLE001
        print("    [의안 일정 조회 실패] %s" % str(e)[:80])
        return None
    if not rows:
        return None
    x = rows[0]
    return {"transfer": ymd(g(x, "GVRN_TRSF_DT")), "promulgation": ymd(g(x, "PROM_DT")),
            "promulgation_no": str(g(x, "PROM_NO")), "plenary_date": ymd(g(x, "RGS_RSLN_DT")),
            "plenary_result": g(x, "RGS_CONF_RSLT")}


def parse_bill_summary_html(html):
    """likms billInfo 탭 조각 -> '제안이유 및 주요내용' 텍스트 (<pre id="prntSummary">)."""
    m = re.search(r'(?is)<pre[^>]+id=["\']prntSummary["\'][^>]*>(.*?)</pre>', html)
    if not m:
        return ""
    t = htmllib.unescape(re.sub(r"(?s)<[^>]+>", " ", m.group(1)))
    t = re.sub(r"^\s*제안이유\s*및\s*주요내용\s*", "", t)
    return re.sub(r"\s+", " ", t).strip()[:TEXT_LIMIT * 2]


def fetch_bill_summary(bill_id):
    """의안정보시스템(likms)의 제안이유 및 주요내용.
    상세페이지는 세션+CSRF 토큰이 필요한 AJAX 구조라 (1) 페이지로 토큰/숨은 입력값 확보 -> (2) billInfo 탭을 POST 로 받는다.
    실패 시 ''."""
    try:
        s = requests.Session()
        s.headers.update(_LIKMS_UA)
        page = s.get("%s/bill/bi/billDetailPage.do" % LIKMS, params={"billId": bill_id, "currMenuNo": "2600044"}, timeout=30)
        h = page.text
        tok = re.search(r'name="_csrf" content="([^"]+)"', h)
        if not tok:
            print("    [의안 본문 실패] CSRF 토큰을 찾지 못함 (HTTP %s)" % page.status_code)
            return ""
        form = dict(re.findall(r'<input type="hidden" id="(\w+)" name="\w+" value="([^"]*)"', h))
        form.setdefault("billId", bill_id)
        r = s.post(LIKMS + "/bill/bi/bill/detail/billInfo.do", data=form, timeout=30,
                   headers={"X-CSRF-TOKEN": tok.group(1), "Referer": page.url, "Origin": LIKMS, "Accept": "*/*"})
        if r.status_code != 200:
            print("    [의안 본문 실패] billInfo HTTP %s" % r.status_code)
            return ""
        return parse_bill_summary_html(r.text)
    except Exception as e:  # noqa: BLE001
        print("    [의안 본문 실패] %s" % str(e)[:80])
        return ""


def fetch_bill_detail(row, text=True, dates=True):
    """-> {'text': 제안이유 및 주요내용, 'transfer','promulgation','promulgation_no'}. 둘 다 못 얻으면 None.
    text=False 면 likms 를 호출하지 않는다(일정만 최신화)."""
    bid = str(g(row, "BILL_ID"))
    if not bid:
        return None
    det = {"text": "", "transfer": "", "promulgation": "", "promulgation_no": ""}
    if dates:
        d = fetch_bill_dates(bid)
        if d:
            det.update({k: d[k] for k in ("transfer", "promulgation", "promulgation_no")})
    if text:
        det["text"] = fetch_bill_summary(bid)
    if not (det["text"] or det["transfer"] or det["promulgation"]):
        return None
    return det


# ============================================================================ 법령: 국가법령정보센터
def _law_headers():
    h = dict(UA)
    ref = os.environ.get("LAW_REFERER")
    if ref:
        h["Referer"] = ref
    return h


def fetch_law_rows(oc, since, until, display=100, max_pages=80):
    """공포일자(ancYd) 범위 내 법령 목록. 연혁(1)/시행예정(2)/현행(3) 모두 조회 후 일련번호로 중복 제거."""
    seen, out = set(), []
    rng = "%s~%s" % (since.replace("-", ""), until.replace("-", ""))
    for nw in ("3", "2", "1"):
        page = 1
        while page <= max_pages:
            r = http_get(LAW_SEARCH, params={"OC": oc, "target": "law", "type": "JSON", "display": display,
                                              "page": page, "sort": "ddes", "ancYd": rng, "nw": nw},
                         headers=_law_headers())
            try:
                data = r.json()
            except Exception:
                raise SourceError("법령 API 응답이 JSON이 아닙니다: " + r.text[:100])
            if "result" in data and "LawSearch" not in data:
                raise SourceError("법령 API 오류: %s / %s" % (data.get("result"), data.get("msg", "")))
            body = data.get("LawSearch", {})
            rows = body.get("law") or []
            if isinstance(rows, dict):
                rows = [rows]
            total = int(str(body.get("totalCnt", "0") or "0"))
            for row in rows:
                mst = str(g(row, "법령일련번호"))
                if mst and mst not in seen:
                    seen.add(mst)
                    out.append(row)
            if not rows or page * display >= total:
                break
            page += 1
    return out


def fetch_law_detail(oc, mst):
    """제개정이유 + 개정문(없으면 조문 앞부분) 텍스트. 실패 시 ''."""
    try:
        r = http_get(LAW_SERVICE, params={"OC": oc, "target": "law", "MST": mst, "type": "JSON"},
                     headers=_law_headers(), timeout=40, tries=2)
        data = r.json()
    except Exception as e:  # noqa: BLE001
        print("    [법령 상세 실패] %s" % str(e)[:80])
        return ""
    law = data.get("법령", data)
    parts = []
    for k in ("제개정이유", "개정문"):
        if k in law:
            parts += flatten_text(law[k])
    if not parts and "조문" in law:
        parts = flatten_text(law["조문"])
    return re.sub(r"\s+", " ", " ".join(parts)).strip()[:TEXT_LIMIT * 2]


# ============================================================================ Gemini 프롬프트
GUIDE = """[분류 기준]
1. ENV_REG (환경규제)
- 대상: 온실가스 목표관리제, 환경정보공개제도, 환경성적표지, 그린워싱(부당한 환경성 표시·광고) 관련 규제.
- 판단: 기업의 온실가스 배출량 관리·보고, 환경정보 등록, 친환경 인증 기준, 그린워싱 처벌 및 가이드라인에 직접적인 영향을 미치는 경우 선택. (단순 자연보호, 지자체 환경미화 등은 제외)

2. DISC_MANDATE (의무공시)
- 대상: 지속가능성 공시(KSSB 제2호 기후 관련 공시 등), 자연자본공시(생물다양성 등) 관련 제도.
- 판단: 자본시장법, 외부감사법 등에 따른 기업의 ESG·기후·자연자본 정보 공시 의무, 공시 범위(Scope 등), 공시 시기 및 위반 제재와 관련된 경우 선택.

3. SOC_CONTRIB (사회공헌)
- 대상: 기업의 공익법인 기부금 후원 및 공익법인의 목적사업 수행 관련 규제·세제.
- 판단: 법인세법상 기부금 손금산입, 상증세법상 공익법인의 출연재산 의무사용·공시·사후관리, 기부금품 모집 및 사용에 관한 법률 개정 사항인 경우 선택.

4. IRRELEVANT (해당없음)
- 대상: 위 1~3번 기준에 명확히 부합하지 않는 모든 법령 및 의안.
- 판단: 'ESG', '친환경', '사회적 가치' 등의 단어가 포함되어 있더라도, 위 3가지 실무 영역과 직접적인 관련이 없다면 반드시 IRRELEVANT로 분류.

[주의사항]
- 하나의 의안/법령이 여러 영역에 걸칠 경우, 기업의 법적 의무나 제재가 가장 직접적으로 발생하는 주된 영역 1개만 선택.
- 추측하지 말고 제공된 텍스트의 명확한 근거만을 바탕으로 판단."""


def build_triage_prompt(items):
    L = ["당신은 기업 ESG 실무 담당자를 돕는 법령 선별 보조자입니다.",
         "아래 법령/의안의 '이름과 기본정보'만 보고, 다음 3개 영역과 관련될 가능성이 있는지 1차로 거릅니다.",
         "- 환경규제(온실가스·환경정보공개·환경성적표지·친환경 인증·그린워싱/부당 표시광고)",
         "- 의무공시(지속가능성·기후·자연자본 공시, 자본시장법·외부감사법상 공시)",
         "- 사회공헌(공익법인·기부금 세제, 법인세법 기부금, 상증세법 공익법인, 기부금품법)",
         "제목만으로 판단이 어렵지만 위 영역을 다룰 수 있는 법(세법, 자본시장법, 환경 관련 법, 표시광고법 등)은 maybe=true 로 합니다.",
         "명백히 무관(형사·가사·국방·교육과정·지역 지정·일반 행정조직 등)하면 maybe=false 입니다.",
         "", "[항목]"]
    for i, it in enumerate(items, 1):
        L.append("%d. [%s] %s | %s" % (i, it.get("kind", ""), it.get("name", ""), it.get("meta", "")))
    L += ["", '출력: JSON 배열만. 예: [{"id":1,"maybe":true},{"id":2,"maybe":false}]  (id는 위 번호, 모든 항목 포함)']
    return "\n".join(L)


def build_classify_prompt(items):
    L = ["당신은 기업 ESG 실무 담당자를 돕는 법령 분류 보조자입니다. 아래 기준에 따라 각 항목을 분류하고 요약하세요.",
         "", GUIDE, "", "[항목]"]
    for i, it in enumerate(items, 1):
        L.append("--- %d ---" % i)
        L.append("구분: %s" % it.get("kind", ""))
        L.append("이름: %s" % it.get("name", ""))
        if it.get("meta"):
            L.append("기본정보: %s" % it["meta"])
        L.append("본문(%s): %s" % (it.get("text_label", "본문"), it.get("text", "")[:TEXT_LIMIT]))
    L += ["",
          "출력: JSON 배열만. 항목당 객체 1개, 필드는 다음과 같습니다.",
          '{"id": 번호, "category": "ENV_REG|DISC_MANDATE|SOC_CONTRIB|IRRELEVANT",',
          ' "evidence": "분류 근거가 되는 문장을 본문에서 글자 그대로 인용(60자 이내). IRRELEVANT면 빈 문자열",',
          ' "reason": "분류 이유 한 줄",',
          ' "background": "제·개정 배경 1~2문장. 본문에 있는 내용만, 없으면 빈 문자열",',
          ' "summary": "주요 내용 핵심 2~4개를 \' / \' 로 구분해 간결하게. 본문에 있는 내용만"}',
          "IRRELEVANT 이면 background/summary 는 빈 문자열로 둡니다. 모든 항목을 포함하세요."]
    return "\n".join(L)


def triage(items, call):
    """-> list[bool|None] (None: 호출/응답 실패 -> 다음 실행에서 재시도)"""
    raw = call(build_triage_prompt(items), timeout=120)
    res = clf.parse_response(raw)
    if res is None:
        return None
    out = []
    for i in range(1, len(items) + 1):
        row = res.get(i)
        out.append(None if row is None else bool(row.get("maybe")))
    return out


def classify(items, call):
    """-> list[dict|None]: {'category','reason','background','summary','evidence'} / None(실패). 근거 미확인 시 IRRELEVANT."""
    raw = call(build_classify_prompt(items), timeout=180)
    res = clf.parse_response(raw)
    if res is None:
        return None
    out = []
    for i, it in enumerate(items, 1):
        row = res.get(i)
        if row is None:
            out.append(None)
            continue
        cat = str(row.get("category", "")).strip().upper()
        if cat not in CATEGORIES:
            out.append({"category": "IRRELEVANT", "reason": str(row.get("reason", ""))[:120], "background": "", "summary": ""})
            continue
        hay = (it.get("name", "") + " " + it.get("text", ""))
        if not clf.evidence_in_text(row.get("evidence"), hay):
            out.append({"category": "IRRELEVANT", "background": "", "summary": "",
                        "reason": "근거 인용을 원문에서 확인하지 못해 제외(%s)" % str(row.get("reason", ""))[:60]})
            continue
        out.append({"category": cat, "reason": str(row.get("reason", ""))[:160],
                    "background": str(row.get("background", "")).strip()[:400],
                    "summary": str(row.get("summary", "")).strip()[:700]})
    return out


# ============================================================================ 레코드 생성
def derive_bill_stage(rec):
    """접수 -> 본회의 심의 -> 정부이송 -> 공포 중 도달한 가장 마지막 단계."""
    if rec.get("promulgation_date"):
        return "공포"
    if rec.get("transfer_date"):
        return "정부이송"
    if rec.get("plenary_date") or rec.get("plenary_result"):
        return "본회의 심의"
    return "접수"


def apply_bill_row(rec, row):
    rec["bill_no"] = str(g(row, "BILL_NO"))
    rec["name"] = g(row, "BILL_NAME")
    # 열린국회정보 TVBPMBILL11 실제 필드: CURR_COMMITTEE(소관위), PROC_DT/PROC_RESULT_CD(본회의 의결일/결과), LINK_URL
    rec["committee"] = g(row, "CURR_COMMITTEE", "COMMITTEE", default=rec.get("committee", ""))
    rec["proposer"] = g(row, "PROPOSER", default=rec.get("proposer", ""))
    rec["age"] = str(g(row, "AGE"))
    rec["receipt_date"] = ymd(g(row, "PROPOSE_DT"))
    rec["plenary_date"] = ymd(g(row, "PROC_DT")) or rec.get("plenary_date", "")
    rec["plenary_result"] = g(row, "PROC_RESULT_CD", "PROC_RESULT", default=rec.get("plenary_result", ""))
    rec["link"] = g(row, "LINK_URL", "DETAIL_LINK") or (LIKMS + "/bill/bi/billDetailPage.do?billId=" + str(g(row, "BILL_ID")))
    rec["stage"] = derive_bill_stage(rec)


def apply_bill_detail(rec, det):
    if not det:
        return
    if det.get("transfer"):
        rec["transfer_date"] = det["transfer"]
    if det.get("promulgation"):
        rec["promulgation_date"] = det["promulgation"]
    if det.get("promulgation_no"):
        rec["promulgation_no"] = det["promulgation_no"]
    rec["stage"] = derive_bill_stage(rec)


PASSED = ("원안가결", "수정가결")


def fill_promulgation_from_laws(rec, law_rows):
    """상세 조회에서 공포일을 못 얻은 가결 의안은 법령 목록(공포일자)으로 보완: 같은 법률명 + 의결일 이후 최초 공포."""
    if rec.get("promulgation_date") or rec.get("plenary_result") not in PASSED or not rec.get("plenary_date"):
        return
    target = bill_target_law(rec.get("name", ""))
    if not target:
        return
    cands = [ymd(g(r, "공포일자")) for r in law_rows
             if g(r, "법령명한글") == target and g(r, "법령구분명") == "법률"
             and ymd(g(r, "공포일자")) >= rec["plenary_date"]]
    if cands:
        rec["promulgation_date"] = min(cands)
        rec["promulgation_source"] = "법령정보 공포일자 매칭"
        rec["stage"] = derive_bill_stage(rec)


def make_law_record(row, cls):
    mst = str(g(row, "법령일련번호"))
    return {"id": mst, "law_id": str(g(row, "법령ID")), "name": g(row, "법령명한글"),
            "kind": g(row, "법령구분명"), "ministry": g(row, "소관부처명"),
            "revision_type": g(row, "제개정구분명"), "promulgation_date": ymd(g(row, "공포일자")),
            "promulgation_no": str(g(row, "공포번호")), "effective_date": ymd(g(row, "시행일자")),
            "status": g(row, "현행연혁코드"), "category": cls["category"], "reason": cls["reason"],
            "background": cls["background"], "summary": cls["summary"], "link": LAW_BODY_URL % mst}


# ============================================================================ 저장소
def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def save_json(path, obj, compact=False):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=None if compact else 2, separators=(",", ":") if compact else None)


class Store:
    def __init__(self, d):
        self.dir = d
        self.bills = load_json(os.path.join(d, "bills.json"), [])
        self.laws = load_json(os.path.join(d, "laws.json"), [])
        st = load_json(os.path.join(d, "state.json"), {})
        self.bills_done = set(st.get("bills_done", []))
        self.laws_done = set(st.get("laws_done", []))
        self.bills_maybe = set(st.get("bills_maybe", []))   # 1차 통과, 본문 판별 대기
        self.laws_maybe = set(st.get("laws_maybe", []))
        self.bill_idx = {b["id"]: b for b in self.bills}
        self.law_idx = {x["id"]: x for x in self.laws}

    def save(self, meta_extra=None):
        self.bills.sort(key=lambda b: (b.get("receipt_date", ""), b.get("bill_no", "")), reverse=True)
        self.laws.sort(key=lambda x: (x.get("promulgation_date", ""), x.get("id", "")), reverse=True)
        save_json(os.path.join(self.dir, "bills.json"), self.bills)
        save_json(os.path.join(self.dir, "laws.json"), self.laws)
        save_json(os.path.join(self.dir, "state.json"),
                  {"bills_done": sorted(self.bills_done), "laws_done": sorted(self.laws_done),
                   "bills_maybe": sorted(self.bills_maybe - self.bills_done),
                   "laws_maybe": sorted(self.laws_maybe - self.laws_done)}, compact=True)
        meta = load_json(os.path.join(self.dir, "meta.json"), {})
        meta.update({"last_updated": dt.datetime.now(KST).isoformat(timespec="seconds"),
                     "bills_total": len(self.bills), "laws_total": len(self.laws)})
        meta.update(meta_extra or {})
        save_json(os.path.join(self.dir, "meta.json"), meta)


# ============================================================================ 처리 파이프라인
DEADLINE = None          # 실행 시간 상한(epoch초). 넘으면 새 작업을 시작하지 않고 저장 후 종료
MAX_DETAIL_FAILS = 8     # 본문 확보가 연속 이 횟수 실패하면 해당 출처 본문 수집을 중단(차단/구조 변경 의심)


def expired():
    return DEADLINE is not None and time.time() > DEADLINE


class Budget:
    def __init__(self, n):
        self.left = n
        self.used = 0

    def take(self):
        if self.left <= 0 or expired():
            return False
        self.left -= 1
        self.used += 1
        return True


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def run_pipeline(kind, cands, store, call, budget, detail_fn, make_rec, done_set, maybe_set, rec_list, rec_idx,
                 text_label, meta_fn, save_cb):
    """cands: [(id, row, name)] (최신순 정렬 완료). 반환: dict(통계)"""
    stat = {"cands": len(cands), "triage_no": 0, "relevant": 0, "irrelevant": 0, "no_text": 0, "pending": 0}
    # 1단계: 이름 기준 선별
    maybe = [c for c in cands if c[0] in maybe_set]          # 지난 실행에서 1차 통과한 항목
    cands = [c for c in cands if c[0] not in maybe_set]
    for batch in chunks(cands, TRIAGE_BATCH):
        if not budget.take():
            stat["pending"] += len(batch)
            continue
        items = [{"kind": kind, "name": n, "meta": meta_fn(r)} for (_i, r, n) in batch]
        res = triage(items, call)
        if res is None:
            stat["pending"] += len(batch)
            continue
        for (i, r, n), ok in zip(batch, res):
            if ok is None:
                stat["pending"] += 1
            elif ok:
                maybe.append((i, r, n))
                maybe_set.add(i)
            else:
                done_set.add(i)
                stat["triage_no"] += 1
        save_cb()
    # 2단계: 본문 기준 확정
    fails = 0
    for batch in chunks(maybe, CLASSIFY_BATCH):
        if budget.left <= 0 or expired() or fails >= MAX_DETAIL_FAILS:
            stat["pending"] += len(batch)
            continue
        items, keep = [], []
        for (i, r, n) in batch:
            if expired() or fails >= MAX_DETAIL_FAILS:
                stat["pending"] += 1
                continue
            det = detail_fn(r)
            text = det.get("text") if isinstance(det, dict) else det
            if not text:
                stat["no_text"] += 1   # 본문 확보 실패 -> 판단 보류(다음 실행에서 재시도)
                fails += 1
                if fails == MAX_DETAIL_FAILS:
                    print("  [중단] %s 본문 수집이 연속 %d회 실패 -> 이번 실행에서는 본문 단계를 건너뜁니다 (--diagnose 로 확인)" % (kind, fails))
                continue
            fails = 0
            items.append({"kind": kind, "name": n, "meta": meta_fn(r), "text": text, "text_label": text_label})
            keep.append((i, r, n, det))
        if not items:
            continue
        budget.take()
        res = classify(items, call)
        if res is None:
            stat["pending"] += len(items)
            continue
        for (i, r, n, det), cls in zip(keep, res):
            if cls is None:
                stat["pending"] += 1
                continue
            done_set.add(i)
            if cls["category"] == "IRRELEVANT":
                stat["irrelevant"] += 1
                continue
            rec = make_rec(r, cls, det)
            rec_list.append(rec)
            rec_idx[i] = rec
            stat["relevant"] += 1
            print("  [%s] %s | %s" % (CATEGORY_LABEL[cls["category"]], n[:40], cls["reason"][:40]))
        save_cb()
    return stat


def bill_meta(row):
    return "소관위 %s | 접수 %s" % (g(row, "CURR_COMMITTEE", "COMMITTEE", default="(미정)"), ymd(g(row, "PROPOSE_DT")))


def law_meta(row):
    return "%s | %s | 소관 %s | 공포 %s" % (g(row, "법령구분명"), g(row, "제개정구분명"), g(row, "소관부처명"),
                                          ymd(g(row, "공포일자")))


def process_bills(args, store, call, budget, assembly_key, save_cb):
    since = args.since
    ages = [22] + ([21] if since < "2024-05-30" else [])
    rows = fetch_bill_rows(assembly_key, ages, since)
    rows = [r for r in rows if is_law_bill(r) and g(r, "BILL_ID")]
    print("[의안] 수집 %d건(법률안, 접수 %s 이후)" % (len(rows), since))

    # 이미 선별된 의안: 단계 최신화 (Gemini 호출 없음)
    by_id = {str(g(r, "BILL_ID")): r for r in rows}
    refreshed = 0
    detail_budget = args.detail_refresh
    for bid, rec in store.bill_idx.items():
        row = by_id.get(bid)
        if not row:
            continue
        before = (rec.get("stage"), rec.get("plenary_date"), rec.get("plenary_result"))
        apply_bill_row(rec, row)
        # 가결됐는데 아직 공포 전이면 정부이송/공포 일정 확인
        if rec.get("plenary_result") in PASSED and not rec.get("promulgation_date") and detail_budget > 0:
            detail_budget -= 1
            apply_bill_detail(rec, fetch_bill_detail(row, text=False))   # 일정만 (likms 호출 없음)
        if before != (rec.get("stage"), rec.get("plenary_date"), rec.get("plenary_result")):
            refreshed += 1
    print("[의안] 기존 선별 의안 %d건 중 상태 변경 %d건" % (len(store.bill_idx), refreshed))

    cands = [(bid, r, g(r, "BILL_NAME")) for bid, r in by_id.items()
             if bid not in store.bills_done and bid not in store.bill_idx]
    cands.sort(key=lambda c: ymd(g(c[1], "PROPOSE_DT")), reverse=True)
    if args.limit:
        cands = cands[:args.limit]
    print("[의안] 신규 판별 대상 %d건" % len(cands))

    def make_rec(row, cls, det):
        rec = {"id": str(g(row, "BILL_ID")), "category": cls["category"], "reason": cls["reason"],
               "background": cls["background"], "summary": cls["summary"],
               "transfer_date": "", "promulgation_date": "", "promulgation_no": ""}
        apply_bill_row(rec, row)
        apply_bill_detail(rec, det if isinstance(det, dict) else None)
        return rec

    def bill_detail(row):
        # 본문(제안이유)은 likms, 정부이송/공포 일정은 가결된 의안만 BILLINFODETAIL 로 조회
        return fetch_bill_detail(row, text=True, dates=g(row, "PROC_RESULT_CD") in PASSED)

    stat = run_pipeline("의안", cands, store, call, budget, bill_detail, make_rec, store.bills_done,
                        store.bills_maybe, store.bills, store.bill_idx, "제안이유 및 주요내용", bill_meta, save_cb)
    return stat, rows


def process_laws(args, store, call, budget, oc, save_cb, bill_rows_for_fill=None):
    rows = fetch_law_rows(oc, args.since, today_kst().isoformat())
    print("[법령] 수집 %d건(공포일 %s 이후)" % (len(rows), args.since))
    by_id = {str(g(r, "법령일련번호")): r for r in rows}
    cands = [(mid, r, g(r, "법령명한글")) for mid, r in by_id.items()
             if mid not in store.laws_done and mid not in store.law_idx]
    cands.sort(key=lambda c: ymd(g(c[1], "공포일자")), reverse=True)
    if args.limit:
        cands = cands[:args.limit]
    print("[법령] 신규 판별 대상 %d건" % len(cands))

    def detail(row):
        return {"text": fetch_law_detail(oc, str(g(row, "법령일련번호")))}

    def make_rec(row, cls, det):
        return make_law_record(row, cls)

    stat = run_pipeline("법령", cands, store, call, budget, detail, make_rec, store.laws_done,
                        store.laws_maybe, store.laws, store.law_idx, "제·개정이유/개정문", law_meta, save_cb)
    return stat, rows


# ============================================================================ 진단
def diagnose(args, assembly_key, oc):
    print("== 진단 모드 (Gemini 호출 없음) ==")
    if args.only in ("all", "bills"):
        print("\n[의안] 서비스: %s" % BILL_SERVICE)
        try:
            r = http_get(ASSEMBLY_BASE + BILL_SERVICE,
                         params={"KEY": assembly_key, "Type": "json", "pIndex": 1, "pSize": 3, "AGE": 22})
            data = r.json()
            rows, total, code, msg = _assembly_rows(data, BILL_SERVICE)
            print("  결과코드:", code, msg, "| 전체 건수:", total)
            if rows:
                print("  필드:", ", ".join(sorted(rows[0].keys())))
                row = rows[0]
                print("  예시:", {k: row.get(k) for k in ("BILL_NO", "BILL_NAME", "CURR_COMMITTEE", "PROPOSE_DT", "PROC_DT", "PROC_RESULT_CD", "LINK_URL")})
                det = fetch_bill_detail(row)
                print("  상세 조회(제안이유+일정):", None if det is None else {k: (v[:80] if isinstance(v, str) else v) for k, v in det.items()})
                if det is None or not det["text"]:
                    print("  [주의] 의안 제안이유 추출 실패 -> likms 접속/CSRF 방식 확인 필요")
            else:
                print("  [주의] 행이 없습니다. 서비스명/키를 확인하세요 (ASSEMBLY_BILL_SERVICE).")
        except Exception as e:  # noqa: BLE001
            print("  [실패]", str(e)[:200])
    if args.only in ("all", "laws"):
        print("\n[법령] lawSearch")
        try:
            r = http_get(LAW_SEARCH, params={"OC": oc, "target": "law", "type": "JSON", "display": 3, "sort": "ddes"},
                         headers=_law_headers())
            data = r.json()
            if "LawSearch" not in data:
                print("  [실패]", data)
            else:
                rows = data["LawSearch"].get("law") or []
                rows = [rows] if isinstance(rows, dict) else rows
                print("  전체 건수:", data["LawSearch"].get("totalCnt"))
                if rows:
                    print("  필드:", ", ".join(sorted(rows[0].keys())))
                    text = fetch_law_detail(oc, str(g(rows[0], "법령일련번호")))
                    print("  제개정이유/개정문 텍스트 길이:", len(text), "|", text[:80])
        except Exception as e:  # noqa: BLE001
            print("  [실패]", str(e)[:200])


# ============================================================================ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="docs/legal")
    ap.add_argument("--since", default="", help="이 날짜 이후만 적재(YYYY-MM-DD). 기본: 오늘-730일")
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--only", choices=["all", "bills", "laws"], default="all")
    ap.add_argument("--max-calls", type=int, default=int(os.environ.get("LEGAL_MAX_CALLS", "150")))
    ap.add_argument("--limit", type=int, default=0, help="출처별 신규 판별 최대 건수(0=전부, 테스트용)")
    ap.add_argument("--detail-refresh", type=int, default=60, help="실행당 공포 확인용 의안 일정 재조회 최대 수")
    ap.add_argument("--max-minutes", type=int, default=int(os.environ.get("LEGAL_MAX_MINUTES", "80")),
                    help="실행 시간 상한(분). 넘으면 새 작업을 멈추고 저장 후 종료(다음 실행에서 이어감)")
    ap.add_argument("--diagnose", action="store_true")
    args = ap.parse_args()
    global DEADLINE
    DEADLINE = time.time() + args.max_minutes * 60
    if not args.since:
        args.since = (today_kst() - dt.timedelta(days=args.days)).isoformat()

    assembly_key = os.environ.get("ASSEMBLY_API_KEY", "")
    oc = os.environ.get("LAW_OC", "")
    if args.diagnose:
        diagnose(args, assembly_key, oc)
        return 0

    store = Store(args.data_dir)
    call = clf.make_gemini_caller()
    budget = Budget(args.max_calls)
    failures = []
    stats = {}
    save_cb = lambda: store.save()  # noqa: E731

    bill_rows = []
    if args.only in ("all", "bills"):
        if not assembly_key:
            failures.append("ASSEMBLY_API_KEY 미설정")
        else:
            try:
                stats["bills"], bill_rows = process_bills(args, store, call, budget, assembly_key, save_cb)
            except Exception as e:  # noqa: BLE001  (SourceError 포함: 실패해도 지금까지 분량은 저장)
                failures.append("의안: %s" % str(e)[:200])
    if args.only in ("all", "laws"):
        if not oc:
            failures.append("LAW_OC 미설정")
        else:
            try:
                stats["laws"], law_rows = process_laws(args, store, call, budget, oc, save_cb)
                for rec in store.bills:
                    fill_promulgation_from_laws(rec, law_rows)
            except Exception as e:  # noqa: BLE001
                failures.append("법령: %s" % str(e)[:200])

    store.save({"since": args.since, "last_stats": stats, "last_failures": failures})
    print("\n==== 완료 ====")
    for k, v in stats.items():
        print("%s: %s" % (k, v))
    if expired():
        print("[알림] 실행 시간 상한(%d분)에 도달해 중간 저장 후 종료했습니다. 다시 실행하면 이어서 진행됩니다." % args.max_minutes)
    print("Gemini 호출 %d회 (상한 %d) | 누적 의안 %d건, 법령 %d건" % (budget.used, args.max_calls, len(store.bills), len(store.laws)))
    pend = sum(s.get("pending", 0) + s.get("no_text", 0) for s in stats.values())
    if pend:
        print("미처리 %d건 -> 다시 실행하면 이어서 진행됩니다." % pend)
    for f in failures:
        print("[경고] " + f)
    return 2 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
