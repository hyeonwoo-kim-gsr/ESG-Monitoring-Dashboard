#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
esg_classifier.py - ESG 기사 감성 분류 공용 모듈 (일일 리포트 / 백필 / 재분류 공용)

기존 방식의 문제와 이 모듈의 해결책
------------------------------------
1) 본문 노이즈: 기존 fetch_article_body 는 HTML 전체에서 태그만 지우고 앞 2000자를 썼음.
   -> <script>/<style>/메뉴/로그인 문구가 앞쪽을 차지하고 진짜 기사 본문은 잘려 나감.
   -> 여기서는 script/style/nav/header/footer/aside 를 제거하고, 기사 본문 영역을 우선 추출하며,
      '저작권자/무단전재/관련기사/많이 본 기사' 이후(사이드바의 무관한 부정 기사 제목 등)는 잘라냄.
2) 인코딩: EUC-KR 사이트를 UTF-8로 잘못 해석하면 깨진 글자만 모델에 들어감 -> 바이트로 받아 직접 디코딩.
3) 프롬프트 관점 모호: '해당 회사에 유리/불리' 는 경쟁사 입장(GS리테일 관점)으로 읽힐 수 있음.
   -> '기사 속 회사 자신의 ESG 평판' 관점으로 못박고, 모델에게 먼저 '사건 유형'(닫힌 목록)을 고르게 함.
   -> POSITIVE/NEGATIVE 는 코드가 사건 유형에서 결정(모델의 label 은 교차검증용).
4) NEGATIVE 안전장치: NEGATIVE 는 기사 원문에서 그대로 인용한 '부정 근거(negative_evidence)'가
   실제 기사 텍스트에 존재할 때만 확정.
5) 판정 정책(소비자 관점): 부정 요소가 없으면 POSITIVE / 부정만 있으면 NEGATIVE /
   의미 있는 긍정+부정이 섞인 경우, 또는 부정으로 봤지만 근거를 원문에서 확인 못한 경우만 UNCERTAIN(보류).
"""
import os
import re
import json
import time
from html.parser import HTMLParser

import requests

# ------------------------------------------------------------------ 사건 유형 → 감성
POSITIVE_TYPES = {
    "ESG_RATING_AWARD": "ESG 평가·등급 우수, 수상, 인증, 지수 편입, 우수기업 선정",
    "COMMUNITY_CSR": "기부·봉사·나눔·후원·김장·취약계층/한부모/아동 지원·지역사회 공헌 행사",
    "PARTNER_SUPPORT": "협력사·중소기업·소상공인·지역 상생, 동반성장, 판로·수출 상담/지원, 지역 행사 협력",
    "ENVIRONMENT_ACTION": "친환경 포장재·재활용·탄소저감·재생에너지·일회용품 감축·ESG 캠페인",
    "EMPLOYEE_WELFARE": "임직원 복지·정서/심리 회복·건강·고용 확대·일자리·안전 강화 프로그램",
    "GOVERNANCE_IMPROVE": "지배구조 개선, ESG 위원회, 투명경영, 정보공개 강화, 상장사 ESG 등급 상향",
}
NEGATIVE_TYPES = {
    "LEGAL_SANCTION": "법 위반 확정·과징금·시정명령·행정처분·압수수색·검찰/공정위 조사·소송 패소",
    "UNFAIR_TRADE": "갑질, 불공정거래, 가맹점주/납품업체 피해, 하도급 문제",
    "SAFETY_ACCIDENT": "안전사고, 산업재해, 식품 안전/위생 문제, 리콜",
    "DATA_BREACH": "개인정보 유출, 해킹, 보안사고",
    "ENV_VIOLATION": "환경오염, 환경 규제 위반, 그린워싱 지적",
    "LABOR_DISPUTE": "부당노동행위, 노사 분쟁, 직장 내 괴롭힘, 임금체불, 근로자 피해",
    "GOVERNANCE_SCANDAL": "횡령·배임·총수 리스크·회계 논란·지배구조 논란",
    "CONTROVERSY": "소비자 피해, 불매운동, 대국민 사과, 사회적 논란",
}
NEUTRAL_TYPES = {
    "NEUTRAL_FACT": "ESG 관련 사실 보도이지만 좋고 나쁨을 판단할 근거가 없음(단순 현황·전망·제도 해설)",
    "NOT_ESG": "ESG와 무관(신제품, 실적, 마케팅, 할인, 상품 트렌드, 르포, 인사, 주가 등)",
}

PAYWALL_MARKERS = [
    "유료 기사", "유료기사", "구독 후 이용", "구독하시면",
    "로그인 후 이용", "회원 전용", "프리미엄 기사",
    "이 기사는 유료", "구독권이 필요",
    "subscribe to read", "premium content", "subscribers only",
]


def is_paywalled(text):
    low = (text or "").lower()
    return any(m.lower() in low for m in PAYWALL_MARKERS)


# ------------------------------------------------------------------ 본문 추출
_SKIP = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form",
         "iframe", "svg", "button", "select", "template"}
_VOID = {"br", "img", "meta", "link", "input", "hr", "area", "base", "col", "embed",
         "source", "track", "wbr", "param"}
_BLOCK = {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section",
          "article", "table", "ul", "ol", "blockquote", "figcaption"}
_BODY_MARK = re.compile(
    r"(article[-_]?(view|body|content|txt|text|cont)|news[-_]?(body|content|text|view|article)|"
    r"dic_area|newsct_article|articlebody|article_body|view[-_]?con|cont_art|read[-_]?body|"
    r"art[-_]?body|news_cont|entry-content)", re.I)
# 이 문구 이후는 기사 끝(저작권/관련기사/인기기사 사이드바) -> 잘라냄
_END_MARK = re.compile(
    r"(저작권자|무단\s*전재|무단\s*복제|Copyright|ⓒ|©|관련\s*기사|많이\s*본\s*(기사|뉴스)|"
    r"인기\s*기사|기사\s*제보|광고문의|구독\s*신청|이\s*기사를\s*공유)")


class _Extractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.skip_level = None
        self.cont_level = None
        self.cont_done = False
        self.in_title = False
        self.title_parts = []
        self.all_text = []
        self.cont_text = []
        self.meta = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key in ("og:title", "og:description", "description"):
                self.meta.setdefault(key, (a.get("content") or "").strip())
            return
        if tag == "br":
            self._add("\n")
            return
        if tag in _VOID:
            return
        self.stack.append(tag)
        if tag == "title":
            self.in_title = True
        if tag in _SKIP and self.skip_level is None:
            self.skip_level = len(self.stack)
        if tag in _BLOCK:
            self._add("\n")
        if (self.skip_level is None and self.cont_level is None and not self.cont_done
                and tag not in ("html", "body", "head")):
            marks = (a.get("id") or "") + " " + (a.get("class") or "")
            if _BODY_MARK.search(marks) or (a.get("itemprop") or "") == "articleBody":
                self.cont_level = len(self.stack)

    def handle_endtag(self, tag):
        if tag in _VOID or tag not in self.stack:
            return
        idx = len(self.stack) - 1 - self.stack[::-1].index(tag)
        del self.stack[idx:]
        if tag == "title":
            self.in_title = False
        if self.skip_level is not None and len(self.stack) < self.skip_level:
            self.skip_level = None
        if self.cont_level is not None and len(self.stack) < self.cont_level:
            self.cont_level = None
            self.cont_done = True
        if tag in _BLOCK:
            self._add("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
            return
        self._add(data)

    def _add(self, s):
        if self.skip_level is not None:
            return
        self.all_text.append(s)
        if self.cont_level is not None:
            self.cont_text.append(s)


def _lines(parts, min_len):
    text = "".join(parts)
    out = []
    for ln in text.split("\n"):
        ln = re.sub(r"\s+", " ", ln).strip()
        if len(ln) >= min_len:
            out.append(ln)
    return out


def _cut_tail(text):
    m = _END_MARK.search(text)
    if m and m.start() >= 80:  # 앞부분에 있는 우연한 일치는 무시
        return text[:m.start()].strip()
    return text


def decode_html(content, header_ct=""):
    """바이트 -> 문자열. 헤더/메타 charset 우선, 실패 시 utf-8 -> cp949."""
    cands = []
    m = re.search(r"charset=([\w\-]+)", header_ct or "", re.I)
    if m:
        cands.append(m.group(1))
    m = re.search(rb"charset=[\"']?([\w\-]+)", content[:4096], re.I)
    if m:
        cands.append(m.group(1).decode("ascii", "ignore"))
    cands += ["utf-8", "cp949"]
    for enc in cands:
        try:
            return content.decode(enc)
        except Exception:
            continue
    return content.decode("utf-8", "replace")


def extract_article(html, max_chars=1500):
    """HTML 문자열 -> {title, meta_description, text, source}"""
    p = _Extractor()
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass
    title = p.meta.get("og:title") or "".join(p.title_parts).strip()
    meta_desc = p.meta.get("og:description") or p.meta.get("description") or ""
    text, source = "", "none"
    cont = _lines(p.cont_text, 20)
    if sum(len(x) for x in cont) >= 150:
        text, source = " ".join(cont), "container"
    else:
        allt = _lines(p.all_text, 40)
        if sum(len(x) for x in allt) >= 150:
            text, source = " ".join(allt), "fallback"
    text = _cut_tail(text)[:max_chars]
    if len(text) < 100 and meta_desc:
        text, source = meta_desc[:max_chars], "meta"
    return {"title": title, "meta_description": meta_desc, "text": text, "source": source}


def fetch_article(url, timeout=10, max_chars=1500):
    """URL -> extract_article 결과. 실패 시 None."""
    try:
        r = requests.get(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept-Language": "ko-KR,ko;q=0.9",
        }, timeout=timeout)
        r.raise_for_status()
        html = decode_html(r.content[:3_000_000], r.headers.get("Content-Type", ""))
        return extract_article(html, max_chars=max_chars)
    except Exception as e:
        print("    [본문 크롤링 실패] " + str(e)[:100])
        return None


def build_content(description, page, max_chars=1500):
    """모델에 줄 내용: 네이버 요약 + 정제 본문. page 가 없으면 요약만."""
    parts = []
    if description:
        parts.append("요약: " + description.strip())
    if page and page.get("text"):
        parts.append("본문: " + page["text"])
    return " / ".join(parts)[:max_chars]


# ------------------------------------------------------------------ 프롬프트
_FEWSHOT = [
    ("GS리테일, 베트남 수출상담회서 196만달러 상담 성과", "PARTNER_SUPPORT", "POSITIVE", "중소 협력사 수출 판로 지원"),
    ("BGF리테일, 서스틴베스트 ESG 평가 최상위 등급", "ESG_RATING_AWARD", "POSITIVE", "ESG 평가 우수"),
    ("현대백화점그룹, 한부모가족에 온정…김장·후원 전달", "COMMUNITY_CSR", "POSITIVE", "취약계층 후원"),
    ("쿠팡·롯데마트, 친환경 포장재 소비 촉진 나선다", "ENVIRONMENT_ACTION", "POSITIVE", "친환경 포장 활동"),
    ("롯데백화점, 현장 근무 직원 정서 회복 프로그램 운영", "EMPLOYEE_WELFARE", "POSITIVE", "직원 정서 지원"),
    ("송파구·롯데백화점, 크리스마스 마켓로 지역 상생", "PARTNER_SUPPORT", "POSITIVE", "지역 상생 행사"),
    ("현대백화점그룹 11개 상장사 ESG 평가 A등급 이상", "ESG_RATING_AWARD", "POSITIVE", "ESG 등급 우수"),
    ("BGF리테일, KCGS ESG 평가 2년 연속 통합 A+", "ESG_RATING_AWARD", "POSITIVE", "ESG 등급 우수"),
    ("[르포] 더마뷰티 키우는 현대백화점, 매장 가보니", "NOT_ESG", "N/A", "상품·매장 트렌드이며 ESG 주제 아님"),
    ("공정위, OO마트 납품업체에 판촉비 떠넘기기…과징금 부과", "LEGAL_SANCTION", "NEGATIVE", "과징금 부과(원문에 명시)"),
    ("OO편의점 점주 사망사고…근로복지공단 산재 인정", "SAFETY_ACCIDENT", "NEGATIVE", "산재 인정 사고"),
    ("OO마트, 공정위 과징금 부과…같은 날 협력사 상생기금 100억 출연 발표", "LEGAL_SANCTION", "MIXED", "과징금(부정)과 상생기금(긍정)이 함께 있음 -> 보류"),
    ("OO백화점, 지속가능경영보고서 발간…탄소배출 로드맵 공개", "GOVERNANCE_IMPROVE", "POSITIVE", "부정 요소 없음 -> 긍정"),
]


def _taxonomy_text():
    lines = ["[긍정 사건 유형 -> POSITIVE]"]
    lines += ["  %s: %s" % (k, v) for k, v in POSITIVE_TYPES.items()]
    lines.append("[부정 사건 유형 -> NEGATIVE]")
    lines += ["  %s: %s" % (k, v) for k, v in NEGATIVE_TYPES.items()]
    lines.append("[중립/제외 유형]")
    lines += ["  %s: %s" % (k, v) for k, v in NEUTRAL_TYPES.items()]
    return "\n".join(lines)


def build_prompt(items):
    """items: [{company, title, content}, ...]  (1건이어도 동일 형식)"""
    L = [
        "당신은 유통업계 ESG 뉴스 클리핑 담당자입니다. 아래 %d개 기사를 각각 독립적으로 분류하세요." % len(items),
        "",
        "===== 가장 중요한 원칙 =====",
        "1. 판단 관점은 '기사에 등장하는 그 회사 자신의 ESG 평판'입니다.",
        "   당신(또는 이 대시보드 사용자)이 경쟁사라는 사실은 절대 고려하지 마세요.",
        "   경쟁사가 상을 받거나 좋은 평가를 받았다면 사용자에게 불리해 보여도 반드시 POSITIVE 입니다.",
        "2. 순서: (a) 기사에서 '실제로 일어난 일'을 평가 없이 한 문장으로 요약 -> (b) 아래 사건 유형 목록에서",
        "   가장 알맞은 것 하나 선택 -> (c) label 은 사건 유형의 극성(긍정/부정/중립)과 일치시킴.",
        "3. NEGATIVE 는 '위반·제재·사고·유출·분쟁·논란·피해'처럼 부정적 사건이 기사에 명시적으로 있을 때만 사용.",
        "   부정 사건이면 negative_evidence 에 그 사건이 적힌 기사 원문 문장/구절을 '한 글자도 바꾸지 말고' 인용.",
        "   그런 구절을 찾을 수 없으면 NEGATIVE 가 아닙니다.",
        "4. 다음은 부정의 근거가 아닙니다: 기사에 '사죄·위기·논란·사고·피해' 같은 단어가 있어도 그 회사가 가해자/위반자가 아닌 경우",
        "   (예: 소비자의 사죄금 기부 미담, 위기 극복 지원, 사고 예방 캠페인, 피해자 돕기 후원), 업계 전반 현황 기사,",
        "   제목 외 본문과 무관한 메뉴·광고·'많이 본 기사' 텍스트.",
        "5. 제목·본문이 모두 ESG(환경·사회·지배구조)와 무관하면 include=false, event_type=NOT_ESG.",
        "   ESG 와 관련 있으나 좋고 나쁨을 가릴 근거가 없으면 NEUTRAL_FACT.",
        "6. 본문이 깨져 있거나 비어 있으면 제목과 요약만으로 판단하되, 근거가 부족하면 NEUTRAL_FACT 로 두세요(추측 금지).",
        "7. 최종 label 은 '일반 소비자가 이 기사를 읽었을 때' 기준으로 정합니다.",
        "   - 소비자가 보기에 부정적인 요소(위반·제재·사고·유출·분쟁·논란·피해)가 기사에 없으면 POSITIVE 입니다.",
        "     (ESG 활동·수상·지원·캠페인뿐 아니라, 부정 요소가 전혀 없는 ESG 관련 기사는 모두 POSITIVE)",
        "   - 부정적 요소만 있으면 NEGATIVE.",
        "   - 같은 기사 안에 의미 있는 긍정 사건과 의미 있는 부정 사건이 '모두' 있을 때에만 MIXED(보류)로 두세요.",
        "     (예: 과징금 부과 + 같은 기사에서 대규모 상생기금 발표). 사과·재발방지 다짐·수습 노력은 긍정 사건이 아니므로",
        "     부정 사건 기사는 그대로 NEGATIVE 입니다. 애매하다는 이유만으로 MIXED 를 쓰지 마세요.",
        "   - has_positive: 기사에 의미 있는 긍정 사건이 있으면 true / has_negative: 부정 사건이 있으면 true.",
        "",
        "===== 사건 유형 목록 =====",
        _taxonomy_text(),
        "",
        "===== 분류 예시 (제목 | event_type | label | 이유) =====",
    ]
    for t, et, lb, why in _FEWSHOT:
        L.append("- %s | %s | %s | %s" % (t, et, lb, why))
    L += [
        "",
        "===== 출력 형식 =====",
        "다음 형식의 JSON 배열만 반환하세요. 다른 텍스트 금지. 모든 id 를 빠짐없이 포함하세요.",
        '[{"id": 1, "include": true|false, "subject_company": "기사 속 주체 회사",',
        '  "what_happened": "실제 일어난 일 한 문장(평가 없이)",',
        '  "event_type": "<사건 유형 코드>",',
        '  "negative_evidence": "기사 원문 그대로 인용한 부정 근거 또는 null",',
        '  "has_positive": true|false, "has_negative": true|false,',
        '  "label": "POSITIVE"|"NEGATIVE"|"MIXED"|"NEUTRAL"|"N/A",',
        '  "reason": "한 문장"}]',
        "",
    ]
    for i, it in enumerate(items, 1):
        L.append("[기사 %d]" % i)
        src = it.get("source_company")
        comp = it.get("company") or ""
        L.append("회사: " + comp + (" (기사 표기: %s)" % src if src and src != comp else ""))
        L.append("제목: " + (it.get("title") or ""))
        L.append("내용: " + (it.get("content") or ""))
        L.append("")
    return "\n".join(L)


# ------------------------------------------------------------------ 응답 파싱·확정
def parse_response(raw):
    """-> {id: row} 또는 None"""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        m = re.search(r"\[[\s\S]*\]", raw) or re.search(r"\{[\s\S]*\}", raw)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except Exception:
            return None
    if isinstance(data, dict):
        lst = [v for v in data.values() if isinstance(v, list)]
        data = lst[0] if lst else [data]
    if not isinstance(data, list):
        return None
    out = {}
    for n, row in enumerate(data, 1):
        if isinstance(row, dict):
            try:
                out[int(row.get("id", n))] = row
            except Exception:
                continue
    return out


def _norm(s):
    return re.sub(r"[\s\"'“”‘’`…·,.\[\]()<>「」『』]", "", s or "")


def evidence_in_text(evidence, text):
    ev, tx = _norm(evidence), _norm(text)
    if len(ev) < 6:
        return False
    if ev in tx:
        return True
    sh = {ev[i:i + 3] for i in range(len(ev) - 2)}
    if not sh:
        return False
    hit = sum(1 for g in sh if g in tx)
    return hit / len(sh) >= 0.8


def finalize(row, title, content):
    """모델 응답 1건 -> (sentiment, info). sentiment: POSITIVE/NEGATIVE/UNCERTAIN/None(제외)"""
    et = str(row.get("event_type", "")).strip().upper()
    label = str(row.get("label", "")).strip().upper()
    info = {"event_type": et, "reason": str(row.get("reason", ""))[:120]}
    if not row.get("include", False) or et in NEUTRAL_TYPES:
        return None, info
    if label in ("NEUTRAL", "N/A") and et not in POSITIVE_TYPES and et not in NEGATIVE_TYPES:
        return None, info

    # 정책: 소비자 입장에서 부정 요소가 (원문에서 확인되는 만큼) 없으면 POSITIVE,
    #       의미 있는 긍정+부정이 함께 있을 때만 UNCERTAIN(보류), 부정만 있으면 NEGATIVE.
    neg_ok = evidence_in_text(row.get("negative_evidence"), (title or "") + " " + (content or ""))
    claims_neg = (et in NEGATIVE_TYPES) or label == "NEGATIVE"
    # 모델이 부정 요소가 있다고 주장하지 않았는데 인용문만 붙어 있는 경우는 근거로 보지 않음
    neg_ok = neg_ok and (claims_neg or label == "MIXED" or bool(row.get("has_negative")))
    # 긍정 유형인데 label 만 NEGATIVE 인 모순: 원문에서 부정 근거가 확인될 때만 부정으로 취급
    if et in POSITIVE_TYPES:
        claims_neg = False
    has_pos = bool(row.get("has_positive")) or label == "MIXED" or et in POSITIVE_TYPES or label == "POSITIVE"
    # 부정 유형/NEGATIVE 라벨의 기사에서 긍정 신호는 has_positive·MIXED·POSITIVE 라벨일 때만 인정
    if et in NEGATIVE_TYPES and label != "POSITIVE" and label != "MIXED":
        has_pos = bool(row.get("has_positive"))

    if neg_ok:
        if has_pos:
            info["note"] = "긍정·부정 요소 혼재 -> 보류"
            return "UNCERTAIN", info
        return "NEGATIVE", info
    # 부정 근거를 원문에서 확인하지 못함
    if claims_neg:
        info["note"] = "부정으로 판단했으나 근거 인용을 원문에서 확인 못함 -> 보류"
        return "UNCERTAIN", info
    # 부정 요소 없음 -> 긍정 (긍정 유형이 아니어도 ESG 관련이며 부정 요소가 없으면 긍정)
    return "POSITIVE", info


def classify_items(items, call_fn, timeout=120):
    """items 일괄 분류. call_fn(prompt, timeout=) -> raw str | None.
    반환: None(호출/파싱 실패) 또는 [(sentiment, info) | None(응답 누락)] (items 순서)"""
    raw = call_fn(build_prompt(items), timeout=timeout)
    res = parse_response(raw)
    if res is None:
        return None
    out = []
    for i, it in enumerate(items, 1):
        row = res.get(i)
        out.append(finalize(row, it.get("title"), it.get("content")) if row else None)
    return out


def classify_single(title, description, url, call_fn, company="", page=None):
    """일일 리포트용 1건 분류. 반환: ('POSITIVE'|'NEGATIVE'|'UNCERTAIN'|None, info)
    유료기사는 (None, {'paywalled': True})."""
    if page is None:
        page = fetch_article(url)
    if page and is_paywalled(page.get("text", "")):
        return None, {"paywalled": True}
    content = build_content(description, page)
    res = classify_items([{"company": company, "title": title, "content": content}], call_fn, timeout=60)
    if res is None or res[0] is None:
        return "UNCERTAIN", {"reason": "Gemini 호출/파싱 실패"}
    return res[0]


# ------------------------------------------------------------------ 독립 Gemini 호출 (재분류/테스트 스크립트용)
_last = [0.0]


def make_gemini_caller(model=None, min_interval=None, max_retries=3):
    key = os.environ["GEMINI_API_KEY"]
    model = model or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
    min_interval = float(os.environ.get("GEMINI_MIN_INTERVAL", "6.0")) if min_interval is None else min_interval
    url = ("https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent?key=%s" % (model, key))

    def call(prompt, timeout=120):
        payload = {"contents": [{"parts": [{"text": prompt}]}],
                   "generationConfig": {"temperature": 0.0, "responseMimeType": "application/json"}}
        backoff = 5
        for attempt in range(max_retries + 1):
            wait = min_interval - (time.time() - _last[0])
            if wait > 0:
                time.sleep(wait)
            _last[0] = time.time()
            try:
                r = requests.post(url, json=payload, timeout=timeout)
                                if r.status_code == 429 or 500 <= r.status_code < 600:
                    if attempt < max_retries:
                        time.sleep(backoff)
                        backoff *= 3
                        continue
                    print("    [Gemini 실패] HTTP %d %s" % (r.status_code, r.text[:160].replace("\n", " ")))
                    return None
                r.raise_for_status()
                return r.json()["candidates"][0]["content"]["parts"][0]["text"]
            except Exception as e:
                if attempt < max_retries:
                    time.sleep(backoff)
                    backoff *= 3
                    continue
                print("    [Gemini 실패] " + str(e)[:80])
                return None
        return None

    return call
