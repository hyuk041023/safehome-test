"""가상 계약서를 점검하고, 계약서로 알 수 없는 서류·현장은 직접 확인할 체크리스트로 안내한다.

11/13 회의 방향
- 직접 분석하는 것은 '가상 계약서' 하나. 등기·건축물대장·선순위 보증금·세금·집 상태는
  사용자가 확인하도록 체크리스트로 안내하고, 확인 전에는 '미확인'으로 둔다.
- 규칙: 금액·항목 누락과 정해진 문구를 점검한다. 규칙에 없는 특약은 '확인 필요'.
- AI(Gemini): 특약에서 확인할 문구 후보를 찾고 쉬운 설명·질문을 만든다(apply_ai).
  AI는 상태를 낮추지 못하고, 계약의 안전·법적 효력을 판단하지 않는다.
  AI가 실패하면 규칙 결과와 미리 써 둔 설명·질문으로 대신한다.
- 결과는 계약서 문구만 본 참고 자료다. 전체 계약의 안전을 보장하지 않는다.
"""
import re
from datetime import date

OK, CAUTION, RISK, UNKNOWN = "정상", "주의", "위험", "확인 필요"
UNCHECKED, SELF_CHECKED = "미확인", "직접 확인함"
SEVERITY = {OK: 0, UNKNOWN: 1, CAUTION: 2, RISK: 3}

# 보증금 비율 계산기 기준 — 팀 MVP 설계 초안 값. 공식 기준이 아니라 참고용이다.
RATIO_CAUTION, RATIO_RISK = 0.70, 0.80

# ── 근거 (11/13: 법령 원문·시행일, 사례 원문 확인 후 연결) ─────────────
# 주택임대차보호법 현행: 법률 제21065호, 2026.1.2 시행 (국가법령정보센터 확인 11/13)
LAW = "https://www.law.go.kr/법령/"
SOURCES = {
    "주임법3": ("주택임대차보호법 제3조 제1항 (대항력: 주택 인도+주민등록 다음 날부터)", LAW + "주택임대차보호법/제3조"),
    "주임법3의2": ("주택임대차보호법 제3조의2 (보증금 우선변제: 대항요건+확정일자)", LAW + "주택임대차보호법/제3조의2"),
    "주임법3의6": ("주택임대차보호법 제3조의6 (확정일자 부여·임대차 정보 열람)", LAW + "주택임대차보호법/제3조의6"),
    "주임법3의7": ("주택임대차보호법 제3조의7 (임대인의 정보 제시 의무: 확정일자 부여 현황·납세증명서)",
                LAW + "주택임대차보호법/제3조의7"),
    "주임법8": ("주택임대차보호법 제8조 (소액임차인 최우선변제)", LAW + "주택임대차보호법/제8조"),
    "주임법10": ("주택임대차보호법 제10조 (강행규정: 임차인에게 불리한 약정은 효력 없음)", LAW + "주택임대차보호법/제10조"),
    "민법623": ("민법 제623조 (임대인은 사용·수익에 필요한 상태를 유지할 의무)", LAW + "민법/제623조"),
    "판례94다34692": ("대법원 1994.12.9. 94다34692 (범위를 정하지 않은 수선 특약은 소규모 수선에 한함)",
                  "https://korea.legal/wiki/94%eb%8b%a434692/"),
    "민법654": ("민법 제654조·제615조 (임차인의 원상회복 의무)", LAW + "민법/제654조"),
    "민법398": ("민법 제398조 제2항 (과다한 손해배상 예정액은 법원이 감액 가능)", LAW + "민법/제398조"),
    "민법114": ("민법 제114조·제130조 (대리·무권대리)", LAW + "민법/제130조"),
    "신탁법4": ("신탁법 제4조 (신탁의 공시와 대항)", LAW + "신탁법/제4조"),
    "국세징수법109": ("국세징수법 제109조 (미납국세 등의 열람)", LAW + "국세징수법/제109조"),
    "건축법79": ("건축법 제79조 (위반 건축물 등에 대한 조치)", LAW + "건축법/제79조"),
    "중개사법25": ("공인중개사법 제25조 (중개대상물의 확인·설명)", LAW + "공인중개사법/제25조"),
}
# 실제 사례 (노션 '전세사기 관련 법률 지식 및 실제 사례 조사'). 참고용이며 사기 판단 근거가 아니다.
CASES = {
    "사칭": ("오피스텔 관리인 사칭 보증금 편취 (파이낸셜뉴스, 2024.10)", "https://www.fnnews.com/news/202410310530507396"),
    "신탁": ("대구 북구 신탁 부동산 무단 임대 17가구 (대구MBC)", "https://dgmbc.com/NewsArticle/604829"),
    "선순위": ("대전 선순위 허위 고지 16억 원 편취 (경향신문, 2026.1)", "https://www.khan.co.kr/article/202601151149001/"),
    "세금": ("임대인 미납 세금으로 세입자 900명 보증금 335억 원 미회수 (한국경제, 2021.10)",
           "https://www.hankyung.com/amp/202110209212e"),
    "위반건축물": ("학생들이 불법건축물인지 모르고 계약하는 현실 (주간경향)", "https://weekly.khan.co.kr/article/202410140600001"),
    "중개사": ("권리관계 허위 설명 중개사 47명 송치 (서울신문)", "https://go.seoul.co.kr/news/newsView.php?id=20240530500030"),
}


def refs(*keys):
    return [SOURCES[k] for k in keys]


# ── 텍스트 읽기 ────────────────────────────────────────────────
def parse_money(s):
    """'금 5,000,000원', '500만원', '1억2천만원', '없음' → 정수(원). 못 읽으면 None."""
    if s is None:
        return None
    s = re.sub(r"\(.*?\)", "", s).replace(",", "").replace(" ", "")
    if "없음" in s:
        return 0
    total, found = 0, False
    for pat, unit in ((r"(\d+)억", 100_000_000), (r"(\d+)천만", 10_000_000), (r"(\d+)만", 10_000)):
        m = re.search(pat, s)
        if m:
            total += int(m.group(1)) * unit
            s = s[m.end():]
            found = True
    m = re.search(r"\d+", s)
    if m and not found:
        return int(m.group())
    if m and found:
        total += int(m.group())
    return total if found else None


def parse_date(s):
    if not s:
        return None
    m = re.search(r"(\d{4})[.\-/년]\s*(\d{1,2})[.\-/월]\s*(\d{1,2})", s)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _key_values(text):
    """'키: 값' 형태의 줄을 (정규화된 키, 값, 원문 줄) 목록으로."""
    out = []
    for line in (text or "").splitlines():
        if ":" in line or "：" in line:
            k, v = re.split(r"[:：]", line, maxsplit=1)
            out.append((k.replace(" ", "").strip(), v.strip(), line.strip()))
    return out


def parse_contract(text):
    f = dict(lessor=None, agent=None, address=None, deposit=None, rent=None, maint=None, period=None,
             written=None, value=None, prior=None, tax=None, specials=[], lines={})
    for k, v, line in _key_values(text):
        if k.startswith("임대인"):
            f["lessor"] = re.split(r"[\s(]", v)[0] or None
            f["lines"]["lessor"] = line
        elif k.startswith("대리인"):
            f["agent"] = v or None
            f["lines"]["agent"] = line
        elif k.startswith("소재지") or k.startswith("주소"):
            f["address"] = v or None
        elif "선순위" in k:
            f["prior"] = parse_money(v)
            f["lines"]["prior"] = line
        elif k.startswith("보증금"):
            f["deposit"] = parse_money(v)
            f["lines"]["deposit"] = line
        elif k.startswith("월세") or k == "차임":
            f["rent"] = parse_money(v)
        elif k.startswith("관리비"):
            f["maint"] = parse_money(v)
        elif k.startswith("계약기간") or k.startswith("임대차기간"):
            f["period"] = v if len(re.findall(r"\d{4}", v)) >= 2 else None
            f["lines"]["period"] = line
        elif "시세" in k:
            f["value"] = parse_money(v)
            f["lines"]["value"] = line
        elif "납세" in k:
            f["tax"] = v or None
            f["lines"]["tax"] = line
        elif k.startswith("작성일"):
            f["written"] = parse_date(v)
    in_specials = False
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("[") and "특약" in s:
            in_specials = True
            continue
        if in_specials and s.startswith("["):
            in_specials = False
        if in_specials and re.match(r"^\d+[.)]", s):
            f["specials"].append(re.sub(r"^\d+[.)]\s*", "", s))
    f["has_special_section"] = "특약" in (text or "")
    return f


# ── 특약 규칙 ─────────────────────────────────────────────────
# 모든 패턴이 들어 있으면 해당. unless 패턴이 하나라도 있으면 그 규칙은 건너뛴다.
NEG = r"(하지\s*않|않는다|않기로|불가|금지|못\s*한|못한|자제|하지\s*말|미룬|미루|보류)"
SPECIAL_RULES = [
    dict(type="전입신고 제한", status=RISK,
         all=[r"(전입|주민등록|주소\s*(이전|를\s*옮|옮))", NEG + r"|옮기지"],
         why="전입신고(주민등록)를 못 하면 대항력과 보증금 우선변제를 갖추기 어려워 보증금을 지키기 힘들 수 있어요.",
         question="전입신고를 하지 말라는 이유가 무엇인가요? 이 특약을 빼 주실 수 있나요?",
         sources=["주임법3", "주임법3의2"]),
    dict(type="보증금 반환 지연", status=RISK,
         all=[r"((새|다음|신규|후속|다른)\s*(임차인|세입자)|새로운\s*(임차인|세입자)|방이\s*(나가|빠지)|재계약|후임)",
              r"(반환|돌려|지급)"],
         why="보증금 반환을 다음 세입자 입주에 묶어 두면 계약이 끝나도 돌려받지 못하거나 늦어질 수 있어요.",
         question="계약 종료일에 새 임차인과 관계없이 보증금을 돌려주신다고 특약을 바꿔 주실 수 있나요?",
         sources=["주임법10", "주임법3의2"]),
    dict(type="담보 추가 설정 허용", status=RISK,
         all=[r"(근저당|대출|담보|저당)", r"(동의|허용|설정할\s*수|받을\s*수|이의를\s*제기하지|이의\s*없)"],
         unless=[r"설정하지\s*않", r"받지\s*않"],
         why="계약 뒤에 집주인이 대출을 더 받으면 내 보증금보다 먼저 갚을 빚이 늘어날 수 있어요.",
         question="계약 기간 중 추가 대출이나 근저당 설정을 하지 않는다는 특약으로 바꿀 수 있나요?",
         sources=["주임법3의2"]),
    dict(type="수리비 전가", status=CAUTION,
         all=[r"(수리|수선|고장|교체)", r"(임차인|세입자)", r"(부담|책임|지불|낸다|처리)"],
         unless=[r"(고의|과실|부주의)"],
         why="수리 범위를 정하지 않고 세입자에게 넘기면 보일러·배관 같은 큰 고장 비용까지 다툼이 생길 수 있어요.",
         question="임차인이 부담하는 수리 범위를 소모품 교체와 제 과실로 생긴 고장으로 한정해 주실 수 있나요?",
         sources=["민법623", "판례94다34692"]),
    dict(type="과도한 원상복구", status=CAUTION,
         all=[r"원상\s*(복구|회복)", r"(도배|장판|전체|모든|전부|새것|신품|입주\s*전\s*상태)"],
         unless=[r"(통상|자연)\s*(적인)?\s*(마모|손모|사용)"],
         why="자연스럽게 낡은 부분까지 세입자 비용으로 고치게 하는 조항일 수 있어요.",
         question="통상적인 사용으로 생긴 마모는 원상복구 대상에서 빼 주실 수 있나요?",
         sources=["민법654"]),
    dict(type="중도 해지 위약금", status=CAUTION,
         all=[r"(중도|중간에|기간\s*중|만기\s*전)", r"(해지|퇴실|나가|이사|해약)",
              r"(위약금|공제|보증금에서|배상|\d+\s*(개월|달)\s*분)"],
         why="중도 퇴실 때 공제하는 금액이 크면 손해가 커질 수 있어요. 공제 기준과 상한이 정해져 있는지 보세요.",
         question="중도 퇴실 시 공제 금액의 기준과 상한(예: 중개수수료 실비)을 정해 주실 수 있나요?",
         sources=["민법398"]),
]
# 세입자를 보호하거나, 흔한 생활 규칙이라 확인 필요로 보내지 않는 문장 (위험 규칙보다 먼저 본다)
KNOWN_OK_RULES = [
    dict(type="담보 설정 금지 (세입자 보호)", all=[r"(담보|근저당|저당|대출)", r"(설정하지\s*않|받지\s*않)"],
         why="계약 뒤 집주인이 담보를 더 잡지 않겠다는, 세입자에게 유리한 문장이에요."),
    dict(type="보증금 미반환 시 해제 (세입자 보호)", all=[r"반환하지\s*못", r"해제"],
         why="보증금을 못 돌려받을 상황이면 계약을 풀 수 있게 한, 세입자에게 유리한 문장이에요."),
    dict(type="임대인 수리 (세입자 보호)", all=[r"(수리|수선)", r"임대인"], none=[r"(임차인|세입자)\s*(이|가|의)?\s*(부담|책임)", NEG, r"이의"],
         why="고장 수리를 집주인이 맡는다는 문장이에요. 민법의 기본 원칙과 같아요."),
    dict(type="과실 수리 (일반 조항)", all=[r"(고의|과실|부주의)", r"(수리|수선|배상|부담)"],
         why="세입자 잘못으로 생긴 고장만 세입자가 부담한다는 흔한 문장이에요."),
    dict(type="전입·확정일자 협조 (세입자 보호)", all=[r"(전입|확정일자)"], none=[NEG],
         why="전입신고·확정일자를 받을 수 있게 한 문장이에요. 입주 당일 바로 하세요."),
    dict(type="통상 마모 제외 (세입자 보호)", all=[r"원상\s*(복구|회복)", r"(통상|자연)\s*(적인)?\s*(마모|손모|사용)"],
         why="자연스럽게 낡은 부분은 원상복구에서 빼 주는, 세입자에게 유리한 문장이에요."),
    dict(type="관리비 포함 항목", all=[r"관리비", r"(포함|별도)"],
         why="관리비에 무엇이 들어가는지 적은 문장이에요. 금액과 항목이 실제 고지서와 같은지만 확인하세요."),
    dict(type="생활 규칙", all=[r"(반려|애완|동물|흡연|담배|주차|쓰레기|분리수거|소음|악기)"],
         why="생활 규칙을 정한 문장이에요. 지킬 수 있는지 스스로 판단하면 돼요."),
]
UNKNOWN_WHY = "등록된 규칙에 없는 문장이라 자동으로 판단하지 않았어요. 원문을 직접 읽고 아래 질문으로 확인하세요."
UNKNOWN_QUESTION = "특약 「{short}」은 어떤 경우에 적용되는지, 제게 불리한 점은 없는지 문서로 설명해 주실 수 있나요?"


def _match(rule, s):
    if not all(re.search(p, s) for p in rule["all"]):
        return False
    if any(re.search(p, s) for p in rule.get("unless", [])):
        return False
    if any(re.search(p, s) for p in rule.get("none", [])):
        return False
    return True


def _short(s, n=24):
    return s if len(s) <= n else s[:n] + "…"


def classify_special(sentence):
    for rule in KNOWN_OK_RULES[:2]:  # 세입자 보호 문장이 위험 규칙에 걸리지 않게 먼저
        if _match(rule, sentence):
            return dict(type=rule["type"], status=OK, why=rule["why"], question=None, sentence=sentence,
                        sources=[], by="규칙")
    for rule in SPECIAL_RULES:
        if _match(rule, sentence):
            return dict(type=rule["type"], status=rule["status"], why=rule["why"], question=rule["question"],
                        sentence=sentence, sources=refs(*rule["sources"]), by="규칙")
    for rule in KNOWN_OK_RULES[2:]:
        if _match(rule, sentence):
            return dict(type=rule["type"], status=OK, why=rule["why"], question=None, sentence=sentence,
                        sources=[], by="규칙")
    return dict(type="규칙에 없는 문장", status=UNKNOWN, why=UNKNOWN_WHY,
                question=UNKNOWN_QUESTION.format(short=_short(sentence)), sentence=sentence, sources=[],
                by="규칙")


# ── AI 결과 합치기 ────────────────────────────────────────────
AI_CATEGORIES = ["전입·주민등록", "보증금 반환", "대출·담보", "수리·관리", "원상복구", "중도 해지·위약금",
                 "관리비·비용", "생활 규칙", "계약 연장·변경", "기타"]
# 계약의 안전·효력을 단정하는 표현이면 AI 설명을 버린다 (11/13: AI는 안전·법적 효력을 확정하지 않음)
AI_BANNED = re.compile(r"(무효(입니다|이에요|예요|다)|불법(입니다|이에요|이다)|사기(입니다|예요|다)|안전(합니다|해요|하다)"
                       r"|문제(가)?\s*없(습니다|어요|다)|효력이\s*없(습니다|어요|다)|유효(합니다|해요|하다))")


def clean_ai_item(item, sentence):
    """Gemini가 준 특약 하나의 설명을 검사한다. 쓸 수 없으면 None."""
    if not isinstance(item, dict):
        return None
    explain = item.get("explain")
    question = item.get("question")
    if not isinstance(explain, str) or not explain.strip() or AI_BANNED.search(explain):
        return None
    if not isinstance(question, str) or AI_BANNED.search(question):
        question = ""
    quote = item.get("quote") if isinstance(item.get("quote"), str) else ""
    quote = quote.strip()
    if quote and quote not in sentence:  # 원문에 없는 인용은 버린다
        quote = ""
    cat = item.get("category") if item.get("category") in AI_CATEGORIES else "기타"
    return dict(concern=item.get("concern") is True, category=cat, quote=quote,
                explain=explain.strip()[:200], question=question.strip()[:150])


def apply_ai(result, ai_items):
    """AI 설명을 특약에 붙인다. AI는 상태를 낮출 수 없고, '정상'을 '확인 필요'로 올리는 것만 할 수 있다."""
    specials = result["items_by_key"]["special"].get("specials", [])
    for idx, ai in (ai_items or {}).items():
        if not isinstance(idx, int) or not 0 <= idx < len(specials):
            continue
        s = specials[idx]
        cleaned = clean_ai_item(ai, s["sentence"])
        if not cleaned:
            continue
        s["ai"] = cleaned
        if cleaned["concern"] and s["status"] == OK:
            s["status"] = UNKNOWN
            s["type"] = f"AI가 찾은 확인 후보 · {cleaned['category']}"
            s["why"] = cleaned["explain"]
            s["question"] = cleaned["question"] or UNKNOWN_QUESTION.format(short=_short(s["sentence"]))
            s["by"] = "AI"
    _finish_special(result["items_by_key"]["special"])
    result["overall"] = overall(result["items"])
    result["questions"] = collect_questions(result["items"], result["checklist"])
    return result


# ── 계약서 분석 항목 ──────────────────────────────────────────
def _item(key, name, status, detail, evidence=None, question=None, sources=None):
    return dict(key=key, name=name, status=status, detail=detail, evidence=evidence, question=question,
                sources=sources or [])


BASICS = [("lessor", "임대인"), ("address", "소재지"), ("deposit", "보증금"), ("rent", "월세"),
          ("period", "계약기간"), ("written", "작성일")]


def _finish_special(it):
    specials = it.get("specials", [])
    if not it.get("has_section"):
        return
    worst = max([s["status"] for s in specials] or [OK], key=SEVERITY.get)
    flagged = [s for s in specials if s["status"] != OK]
    it["status"] = worst
    it["detail"] = ("살펴볼 특약: " + ", ".join(s["type"] for s in flagged)) if flagged else \
        ("특약이 없어요." if not specials else "등록된 위험 유형에 해당하는 특약이 없어요.")


def check(contract_text, kind=None, ai_items=None):
    """가상 계약서 점검. 반환: items(계약서 분석), overall, checklist(직접 확인할 것), questions."""
    c = parse_contract(contract_text)
    items = []

    # 1. 필수 항목
    missing = [label for key, label in BASICS if not c[key]]
    if missing:
        items.append(_item("basics", "필수 항목", UNKNOWN,
                           f"계약서에서 찾지 못했어요: {', '.join(missing)}. 빈칸으로 계약하지 마세요.",
                           question=f"계약서의 {', '.join(missing)} 칸을 채워서 다시 보내 주실 수 있나요?"))
    else:
        items.append(_item("basics", "필수 항목", OK, "임대인·소재지·보증금·월세·계약기간·작성일이 모두 적혀 있어요."))

    # 2. 계약하는 사람
    if c["agent"]:
        items.append(_item("party", "계약하는 사람", CAUTION,
                           f"임대인 본인이 아니라 대리인이 계약해요 ({c['agent']}). 위임장·인감증명서와 소유자 본인 확인이 필요해요.",
                           c["lines"].get("agent"),
                           "소유자 본인의 인감이 찍힌 위임장과 본인발급 인감증명서를 보여 주시고, 소유자와 직접 통화할 수 있을까요?",
                           refs("민법114")))
    elif c["lessor"]:
        items.append(_item("party", "계약하는 사람", OK,
                           f"임대인 '{c['lessor']}' 본인이 계약하는 것으로 적혀 있어요. 등기부 소유자와 같은 사람인지는 아래에서 직접 확인하세요."))
    else:
        items.append(_item("party", "계약하는 사람", UNKNOWN, "임대인이 누구인지 적혀 있지 않아요.",
                           question="임대인(집주인) 이름과 연락처를 계약서에 적어 주실 수 있나요?"))

    # 3. 확인·설명 기재 (선순위 보증금·시세·납세증명서) — 적혀 있는지와 적힌 말만 본다
    gaps = [n for n, v in (("선순위 보증금", c["prior"]), ("건물 시세", c["value"]), ("납세증명서", c["tax"]))
            if v is None]
    tax_not_checked = bool(c["tax"]) and not re.search(r"(확인함|확인\s*완료|제시|열람\s*완료)", c["tax"])
    if tax_not_checked:
        items.append(_item("disclosure", "확인·설명 기재", CAUTION,
                           f"계약서에 납세증명서가 '{c['tax']}'(으)로 적혀 있어요. 집주인 체납 세금은 보증금보다 먼저 빠져나갈 수 있어요.",
                           c["lines"].get("tax"),
                           "임대인의 국세·지방세 납세증명서를 계약 전에 보여 주실 수 있나요?",
                           refs("주임법3의7", "국세징수법109")))
    elif gaps:
        items.append(_item("disclosure", "확인·설명 기재", UNKNOWN,
                           f"중개대상물 확인·설명 부분에 없는 내용: {', '.join(gaps)}.",
                           question=f"확인·설명서에 다음 내용도 적어 주실 수 있나요? ({', '.join(gaps)})",
                           sources=refs("중개사법25", "주임법3의7")))
    else:
        items.append(_item("disclosure", "확인·설명 기재", OK,
                           "선순위 보증금·건물 시세·납세증명서 확인 여부가 적혀 있어요. 적힌 숫자가 맞는지는 아래 서류로 확인하세요."))

    # 4. 특약
    specials = [classify_special(s) for s in c["specials"]]
    sp = _item("special", "특약 문구", UNKNOWN, "특약사항 부분을 찾지 못했어요.",
               question="특약사항이 있다면 전부 계약서에 적어 주실 수 있나요?")
    sp["specials"], sp["has_section"] = specials, c["has_special_section"]
    _finish_special(sp)
    items.append(sp)

    result = dict(items=items, items_by_key={i["key"]: i for i in items}, contract=c,
                  checklist=checklist(c, kind))
    result["overall"] = overall(items)
    result["questions"] = collect_questions(items, result["checklist"])
    if ai_items:
        apply_ai(result, ai_items)
    return result


def overall(items):
    """계약서 문구만 본 종합. '안전·양호'라는 말은 쓰지 않는다."""
    sts = [i["status"] for i in items] + [s["status"] for i in items for s in i.get("specials", [])]
    if RISK in sts:
        return dict(label="계약서에 위험 문구가 있어요", level=RISK)
    if CAUTION in sts:
        return dict(label="계약서에 주의할 문구가 있어요", level=CAUTION)
    if UNKNOWN in sts:
        return dict(label="계약서에 확인할 부분이 있어요", level=UNKNOWN)
    return dict(label="계약서에서 위험 문구를 찾지 못했어요", level=OK)


# ── 직접 확인할 서류·현장 체크리스트 ──────────────────────────
def checklist(c, kind=None):
    """매물 유형과 계약서 내용에 맞춰 직접 확인할 것을 만든다. 상태는 모두 '미확인'에서 시작한다."""
    multi = kind in (None, "원룸", "투룸")  # 원룸·투룸은 대개 다가구주택(건물 하나에 여러 세입자)
    lessor = c["lessor"] or "임대인"
    out = []

    def add(key, title, why, how, where, hint=None, important=False, sources=(), cases=(), ask=None):
        out.append(dict(key=key, title=title, why=why, how=how, where=where, hint=hint, important=important,
                        status=UNCHECKED, sources=refs(*sources), cases=[CASES[k] for k in cases], ask=ask))

    add("registry", "등기사항증명서 (등기부등본)",
        "집주인이 진짜 소유자인지, 집에 빚(근저당)·압류·신탁이 걸려 있는지 계약서만으로는 알 수 없어요.",
        [f"갑구 소유자가 계약서 임대인 '{lessor}'와 같은지" + (" (대리인 계약이면 위임장의 소유자와도)" if c["agent"] else ""),
         "갑구에 압류·가압류·가처분·신탁 등기가 있는지 (신탁이면 신탁원부와 신탁회사 동의서 필요)",
         "을구 근저당 채권최고액 (아래 보증금 비율 계산에 넣기)",
         "임차권등기(이전 세입자가 보증금을 못 받은 흔적)가 있는지",
         "계약 당일과 잔금 직전에 한 번 더 열람"],
        "대법원 인터넷등기소에서 주소로 열람 (누구나 가능)",
        important=True, sources=("신탁법4", "주임법3의2"), cases=("사칭", "신탁"),
        ask="계약 당일 최신 등기사항증명서를 같이 확인할 수 있을까요?")
    if multi:
        add("prior", "선순위 보증금 (확정일자 부여 현황)",
            "다가구주택은 한 건물의 다른 세입자 보증금이 내 보증금보다 먼저 배당될 수 있어요. 계약서에 적힌 선순위 금액이 맞는지 서류로 확인해야 해요.",
            ["임대인에게 확정일자 부여 현황(다른 호실 보증금·확정일자)을 보여 달라고 요청",
             "또는 임대인 동의를 받아 주민센터나 인터넷등기소에서 열람",
             "(근저당 + 선순위 보증금 + 내 보증금) ÷ 건물 시세를 계산"],
            "임대인 제시 · 주민센터 · 인터넷등기소 (임대차 정보 열람)",
            hint=(f"계약서에는 선순위 보증금 {c['prior']:,}원으로 적혀 있어요." if c["prior"] is not None
                  else "계약서에 선순위 보증금이 적혀 있지 않아요."),
            important=True, sources=("주임법3의6", "주임법3의7", "주임법3의2", "주임법8"), cases=("선순위",),
            ask="계약 전에 이 건물의 확정일자 부여 현황(선순위 보증금)을 보여 주실 수 있나요?")
    else:
        add("prior", "이 호실의 선순위 세입자·보증금",
            "오피스텔은 호실마다 등기가 따로 있어요. 이 호실에 먼저 들어온 세입자나 근저당이 있으면 내 보증금 순위가 밀려요.",
            ["등기부 을구 근저당과 임차권등기 확인", "현재 사는 세입자가 있으면 퇴거일과 보증금 반환 여부 확인",
             "확정일자 부여 현황을 임대인에게 요청"],
            "인터넷등기소 · 임대인 제시",
            hint=None, important=False, sources=("주임법3의6", "주임법3의2"), cases=("선순위",),
            ask="이 호실에 현재 세입자나 확정일자를 받은 계약이 있나요?")
    tax_unchecked = not c["tax"] or not re.search(r"(확인함|확인\s*완료|제시|열람\s*완료)", c["tax"] or "")
    add("tax", "집주인 세금 체납 (납세증명서)",
        "집주인이 세금을 안 냈으면 집이 공매로 넘어갈 때 세금이 내 보증금보다 먼저 빠져나갈 수 있어요.",
        ["임대인에게 국세·지방세 납세증명서를 요청 (계약 때 제시 의무)",
         "또는 임대인 동의를 받아 세무서·주민센터에서 미납 세금 열람"],
        "임대인 제시 · 세무서 · 주민센터",
        hint=(f"계약서에는 '{c['tax']}'(으)로 적혀 있어요." if c["tax"] else "계약서에 납세증명서 확인 여부가 없어요."),
        important=tax_unchecked, sources=("주임법3의7", "국세징수법109"), cases=("세금",),
        ask="임대인의 국세·지방세 납세증명서를 보여 주실 수 있나요?")
    add("building", "건축물대장",
        "불법 증축·용도 변경된 건물(위반건축물)은 이행강제금 대상이고, 전세대출·보증보험 가입이 막힐 수 있어요.",
        ["'위반건축물' 표시가 있는지",
         "용도가 주거용인지 (오피스텔은 업무시설로 되어 있어도 주거 사용이 가능한지 확인)" if not multi
         else "다가구주택의 가구 수와 실제 방 개수가 맞는지 (방 쪼개기 여부)",
         "계약서 호수와 대장의 호수가 같은지"],
        "정부24에서 건축물대장 열람 (무료)",
        sources=("건축법79",), cases=("위반건축물",))
    if c["agent"]:
        add("agent", "대리인 권한",
            "소유자가 아닌 사람과 계약하면, 권한이 없을 때 보증금을 돌려받기 어려울 수 있어요.",
            ["소유자 인감이 찍힌 위임장과 본인발급 인감증명서 확인", "소유자 본인과 영상통화·통화로 계약 사실 확인",
             "보증금은 소유자 명의 계좌로만 보내기"],
            "임대인(소유자) 직접",
            hint=f"계약서 대리인 줄: {c['agent']}", important=True, sources=("민법114",), cases=("사칭",),
            ask="소유자분 명의 계좌로 보증금을 보내도 될까요?")
    add("broker", "중개사무소와 확인·설명서",
        "중개사는 권리관계를 확인해 설명할 의무가 있어요. 확인·설명서를 서면으로 받아 두면 나중에 근거가 돼요.",
        ["등록된 중개사무소인지 (국가공간정보포털 부동산중개업 조회)", "중개대상물 확인·설명서를 서면으로 받기"],
        "국가공간정보포털 · 중개사무소",
        sources=("중개사법25",), cases=("중개사",))
    add("site", "현장 상태",
        "문서로는 알 수 없는 집 상태는 직접 봐야 해요. 입주 전 사진을 남기면 퇴실 때 원상복구 다툼을 줄일 수 있어요.",
        ["실제 호수·구조가 계약서와 같은지", "누수·곰팡이 흔적 (천장, 창틀, 화장실)", "보일러·수압·온수",
         "도어락·창문 잠금 (방범)", "밤 시간 소음", "입주 날 집 상태 사진·영상 남기기"],
        "직접 방문 (낮·밤 한 번씩이면 더 좋아요)")
    return out


def deposit_ratio(mortgage, prior, deposit, value):
    """(근저당 + 선순위 보증금 + 내 보증금) ÷ 시세. 값이 하나라도 없으면 None."""
    if None in (mortgage, prior, deposit, value) or not value:
        return None
    ratio = (mortgage + prior + deposit) / value
    level = RISK if ratio >= RATIO_RISK else CAUTION if ratio >= RATIO_CAUTION else OK
    return dict(ratio=ratio, level=level)


def collect_questions(items, checklist_items=()):
    qs = []
    for i in items:
        if i["status"] != OK and i["question"]:
            qs.append(i["question"])
        for s in i.get("specials", []):
            if s["status"] != OK and s["question"]:
                qs.append(s["question"])
    for ck in checklist_items:
        if ck["important"] and ck.get("ask"):
            qs.append(ck["ask"])
    return list(dict.fromkeys(qs))


def count_flags(result):
    """추천 카드 배지용: 계약서 문구 기준 위험·주의·확인 필요 수 (특약은 문장마다 센다)."""
    from collections import Counter
    cnt = Counter()
    for i in result["items"]:
        if i["key"] == "special" and i.get("has_section"):
            cnt.update(s["status"] for s in i["specials"])
        else:
            cnt[i["status"]] += 1
    return {RISK: cnt[RISK], CAUTION: cnt[CAUTION], UNKNOWN: cnt[UNKNOWN]}


# 항목 → 계약서에서 근거가 된 줄
LINE_KEYS = {"party": ["agent"], "basics": []}


def highlights(result):
    """③ 화면용: 위험·주의·확인 필요에 번호를 붙이고, 계약서의 어느 줄인지 정리한다.

    반환 dict(notes=[{no, status, title, detail, question, sentence, sources, ai, by}],
              marks={줄에 들어 있는 문자열: [번호, ...]})
    """
    c = result["contract"]
    notes, marks = [], {}

    def mark(text, no):
        if text:
            marks.setdefault(text.strip(), []).append(no)

    for it in result["items"]:
        if it["key"] == "special" and it.get("has_section"):
            for s in it["specials"]:
                if s["status"] == OK:
                    continue
                no = len(notes) + 1
                notes.append(dict(no=no, status=s["status"], title=f"특약 · {s['type']}", detail=s["why"],
                                  question=s["question"], sentence=s["sentence"], sources=s["sources"],
                                  ai=s.get("ai"), by=s.get("by", "규칙")))
                mark(s["sentence"], no)
            continue
        if it["status"] == OK:
            continue
        no = len(notes) + 1
        notes.append(dict(no=no, status=it["status"], title=it["name"], detail=it["detail"],
                          question=it["question"], sentence=it["evidence"], sources=it["sources"], ai=None,
                          by="규칙"))
        if it["key"] == "disclosure" and it["status"] == CAUTION:
            mark(c["lines"].get("tax"), no)
        else:
            for k in LINE_KEYS.get(it["key"], []):
                mark(c["lines"].get(k), no)
    return dict(notes=notes, marks=marks)


def line_marks(line, doc_marks):
    """한 줄에 붙은 번호 목록 (줄 안에 표시 대상 문자열이 들어 있으면)."""
    s = (line or "").strip()
    if not s:
        return []
    return sorted({n for key, nos in doc_marks.items() if key and key in s for n in nos})


def message_draft(listing_name, questions):
    """집주인·중개사에게 보낼 문자 초안 (템플릿 기반)."""
    if not questions:
        return (f"안녕하세요, {listing_name} 매물 계약 관련해 연락드립니다. 계약 전에 등기사항증명서와 "
                "납세증명서를 함께 확인하고 싶어요. 감사합니다.")
    body = "\n".join(f"{n}. {q}" for n, q in enumerate(questions, 1))
    return (f"안녕하세요, {listing_name} 매물 계약을 고민 중인 학생입니다.\n"
            f"계약 전에 몇 가지 확인하고 싶어 연락드려요.\n\n{body}\n\n"
            "답변 주시면 확인 후 다시 연락드리겠습니다. 감사합니다.")
