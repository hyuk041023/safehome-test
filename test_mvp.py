"""MVP 검증 테스트. 실행: python test_mvp.py

1) 가상 계약서 사례 10종이 모든 매물에서 기대한 종합 문구로 나오는지
2) 정보가 빠진 경우 추측하지 않고 '확인 필요'로 가는지, '안전·양호'라는 말을 쓰지 않는지
3) 설문 가중치 · 우선순위 혼합값이 노션(11/10 회의록) 예시와 같은지
4) 추천 결과가 필수 조건을 100% 만족하는지 (문제 정의 성공 기준)
5) 실거래 시세 비교
6) AI(Gemini) 답을 검사해서 받는지, AI가 실패하면 규칙 해석으로 대신하는지 (Gemini는 흉내만 냄)
7) 계약서 줄 표시
8) 특약 표현 바꾸기: 같은 뜻의 다른 표현을 잡는지, 오탐·미탐 집계 (11/13)
9) AI 특약 설명: 원문에 없는 인용·단정 표현은 버리고, AI가 판정을 낮추지 못하고, 실패하면 규칙으로 대신하는지
10) 직접 확인할 체크리스트: 매물 유형·계약서 내용에 맞게 바뀌고 모두 '미확인'에서 시작하는지
"""
import json
import sqlite3
import tempfile
from pathlib import Path

import ai_parse
import db_seed
import contract_check as cc
import recommend as rc


def load(db):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("SELECT * FROM listings")]
    docs = {r["listing_id"]: r["contract_text"] for r in con.execute("SELECT * FROM contracts")}
    cases = {r["case_id"]: dict(r) for r in con.execute("SELECT * FROM cases")}
    return rows, docs, cases


BANNED_WORDS = ("안전", "양호", "문제없", "문제 없")


def test_cases(rows, docs, cases):
    table, fails = {}, []
    for r in rows:
        res = cc.check(docs[r["id"]], r["kind"])
        exp = cases[r["case_id"]]["expected"]
        table.setdefault(r["case_id"], set()).add(res["overall"]["label"])
        assert not any(w in res["overall"]["label"] for w in BANNED_WORDS), res["overall"]
        if res["overall"]["label"] != exp:
            fails.append((r["id"], r["case_id"], exp, res["overall"]["label"],
                          [(i["name"], i["status"]) for i in res["items"] if i["status"] != cc.OK]))
    print("\n[1] 사례별 기대 결과 (계약서 문구만)")
    for k in sorted(cases):
        ok = table[k] == {cases[k]["expected"]}
        print(f"  {'✅' if ok else '❌'} {k} {cases[k]['title']:<16} 기대: {cases[k]['expected']:<22} 실제: {', '.join(table[k])}")
    for f in fails:
        print("   실패:", f)
    assert not fails


def test_missing_info():
    res = cc.check("임대인: 김가상\n보증금: 금 5,000,000원")
    sts = {i["key"]: i["status"] for i in res["items"]}
    print("\n[2] 정보 부족 문서:", res["overall"]["label"], sts)
    assert res["overall"]["label"] == "계약서에 확인할 부분이 있어요"
    assert sts["basics"] == sts["disclosure"] == sts["special"] == cc.UNKNOWN
    assert sts["party"] == cc.OK
    basics = res["items_by_key"]["basics"]["detail"]
    assert all(w in basics for w in ("소재지", "월세", "계약기간", "작성일")) and "보증금" not in basics.split(":")[1]
    # 빈 문서: 전부 확인 필요, 체크리스트는 그대로 미확인
    empty = cc.check("")
    assert {i["status"] for i in empty["items"]} == {cc.UNKNOWN}
    assert all(ck["status"] == cc.UNCHECKED for ck in empty["checklist"])
    # 사용자가 텍스트를 고쳐 쓴 경우(만원 단위)도 읽는지
    assert cc.parse_money("금 1억2천만원") == 120_000_000
    assert cc.parse_money("500만원") == 5_000_000
    assert cc.parse_money("없음") == 0


def test_weights():
    w = rc.blend_weights(["조용함"])
    rounded = {k: round(v, 2) for k, v in w.items()}
    print("\n[3] '조용함'만 골랐을 때:", rounded)
    assert abs(sum(rc.BASE_WEIGHTS.values()) - 1) < 0.002
    assert abs(sum(w.values()) - 1) < 0.002
    # 노션 11/10 회의록 예시: 조용함 0.50, 가격 0.12, 거리 0.10, 관리비 0.08, 넓이 0.07, 채광 0.06, 옵션 0.06
    assert rounded == {"가격": 0.12, "거리": 0.10, "관리비": 0.08, "넓이": 0.07,
                       "채광": 0.06, "옵션": 0.06, "조용함": 0.50}


def test_filter_and_sentence(rows):
    cond, understood = rc.parse_sentence("충북대 정문 걸어서 10분 안, 월세 40만 원 이하, 조용하고 세탁기 있는 곳")
    print("\n[4] 문장 해석:", understood)
    assert cond["max_rent"] == 400_000 and cond["max_walk"] == 10 and cond["gate"] == "정문"
    assert cond["options"] == ["세탁기"] and cond["priorities"] == ["조용함"]
    hits = rc.filter_listings(rows, cond)
    ranked = rc.score_listings(hits, rc.blend_weights(cond["priorities"]), cond["gate"])
    for s in ranked:
        r = s["row"]
        assert r["rent"] <= 400_000 and r["walk_main"] <= 10 and "세탁기" in r["options"]
    print(f"    조건 만족 매물 {len(hits)}건 → 상위:", [(s['row']['name'], round(s['score'], 3)) for s in ranked[:3]])
    assert len(hits) >= 3, "시연 문장으로 최소 3건은 나와야 함"


def test_market(rows, db):
    """실거래 시세 파일이 있으면: 100건, 모든 매물이 비교 가능, 낮음·비슷·높음이 모두 나와야 함."""
    from collections import Counter
    real = rc.load_market_csv(Path(db_seed.__file__).with_name("market_prices.csv"))
    con = sqlite3.connect(db)
    market = {(d, k): (m, n) for d, k, m, n in con.execute(
        "SELECT dong, kind, median_monthly, sample_n FROM market_prices")}
    labels = Counter(rc.market_compare(r, market)["label"] for r in rows)
    kinds = Counter(r["kind"] for r in rows)
    print(f"\n[5] 매물 {len(rows)}건, 유형 {dict(kinds)}, 시세 비교 {dict(labels)}")
    assert len(rows) == 100
    if real:
        assert labels["비교 불가"] == 0, "실거래 기준이 있으면 모두 비교 가능해야 함"
        assert labels["낮음"] >= 5 and labels["높음"] >= 5 and labels["비슷"] >= 40
        ge = next(r for r in rows if r["dong"] == "개신동" and r["kind"] == "오피스텔")
        detail = rc.market_compare(ge, market)["detail"]
        print("    개신동 오피스텔(거래 1건) →", detail)
        assert rc.AREA_NAME in detail


def test_ai_parse(rows):
    """AI 답이 규칙 해석과 같은 형식으로 바뀌고, 이상한 값은 버려지는지. AI 실패 시 안내 문구가 나오는지."""
    fields = {"max_rent_manwon": 40, "max_deposit_manwon": None, "max_walk_min": 10, "gate": "정문",
              "kinds": ["원룸", "아파트"], "options": ["세탁기", "세탁기", "건조기"],
              "priorities": ["조용함", "가격"]}
    cond, understood = rc.cond_from_ai(fields)
    assert cond == {"max_rent": 400_000, "max_walk": 10, "gate": "정문", "kinds": ["원룸"],
                    "options": ["세탁기"], "priorities": ["조용함", "가격"]}, cond
    rule_cond, _ = rc.parse_sentence("충북대 정문 걸어서 10분 안, 월세 40만 원 이하, 원룸, 조용하고 세탁기, 싼 곳")
    assert rule_cond == cond, ("AI 결과와 규칙 결과의 형식이 같아야 함", rule_cond)
    print("\n[6] AI 답 → 조건:", " · ".join(understood))

    # 이상한 값: 음수·문자열·True·범위 밖·없는 문 → 전부 버림 (추측 금지)
    bad, u = rc.cond_from_ai({"max_rent_manwon": -5, "max_deposit_manwon": "500", "max_walk_min": True,
                              "gate": "남문", "kinds": "원룸", "priorities": None})
    assert bad == {} and u == [], bad
    assert rc.cond_from_ai(None) == ({}, [])

    # AI 조건으로 검색해도 필수 조건 100% 만족
    hits = rc.filter_listings(rows, cond)
    assert hits and all(r["rent"] <= 400_000 and "세탁기" in r["options"] and r["walk_main"] <= 10 for r in hits)

    # /api/parse 호출 흉내: 성공 / 키 없음 / 서버 오류 / 너무 긴 문장
    ai_parse.os.environ["SAFEHOME_API_URL"] = "https://example.test/api/parse"
    ok = lambda url, p: (200, json.dumps({"ok": True, "fields": fields}))
    nokey = lambda url, p: (503, json.dumps({"ok": False, "error": "no_key",
                                             "message": "AI 키가 아직 설정되지 않았어요. 규칙 해석으로 대신할게요."}))
    broken = lambda url, p: (_ for _ in ()).throw(OSError("down"))
    assert ai_parse.request_parse("월세 40", post=ok) == (fields, None)
    f, msg = ai_parse.request_parse("월세 40", post=nokey)
    assert f is None and "키" in msg
    f, msg = ai_parse.request_parse("월세 40", post=broken)
    assert f is None and "연결" in msg
    f, msg = ai_parse.request_parse("가" * 201, post=ok)
    assert f is None and "200자" in msg
    del ai_parse.os.environ["SAFEHOME_API_URL"]
    f, msg = ai_parse.request_parse("월세 40")
    assert f is None and "규칙" in msg  # 주소가 없으면(로컬) 규칙으로
    print("    AI 실패 시 안내:", msg)


def test_highlights(rows, docs):
    """③ 화면의 줄 표시: 표시된 항목마다 번호가 있고, 그 번호가 계약서의 실제 줄에 붙는지."""
    for r in rows:
        res = cc.check(docs[r["id"]], r["kind"])
        h = cc.highlights(res)
        flags = cc.count_flags(res)
        assert len(h["notes"]) == sum(flags.values()), (r["case_id"], h["notes"], flags)
        used = set()
        for line in docs[r["id"]].splitlines():
            used.update(cc.line_marks(line, h["marks"]))
        lined = {n["no"] for n in h["notes"] if n["sentence"]}
        assert used == lined, (r["case_id"], used, h["notes"])
    e = next(r for r in rows if r["case_id"] == "H")
    h = cc.highlights(cc.check(docs[e["id"]], e["kind"]))
    print("\n[7] 계약서 줄 표시 (사례 H):", " / ".join(f"{n['no']}. {n['title']}" for n in h["notes"]))


# 같은 뜻의 다른 표현 (기대 유형). 시연·발표용 오탐·미탐 집계의 기준표.
VARIANTS = [
    # 전입신고 제한 (위험)
    ("임차인은 계약 기간 동안 전입신고를 하지 않는다.", "전입신고 제한"),
    ("임차인은 주민등록을 옮기지 않기로 한다.", "전입신고 제한"),
    ("잔금 후 6개월간 전입신고는 불가하다.", "전입신고 제한"),
    ("임대인 대출 문제로 전입은 자제해 주시기 바랍니다.", "전입신고 제한"),
    ("세입자는 주소 이전을 하지 않는다.", "전입신고 제한"),
    # 보증금 반환 지연 (위험)
    ("보증금은 새 임차인이 입주한 후에 반환한다.", "보증금 반환 지연"),
    ("방이 나가면 보증금을 돌려준다.", "보증금 반환 지연"),
    ("다음 세입자의 보증금을 받아 기존 보증금을 지급한다.", "보증금 반환 지연"),
    ("후임 임차인과 계약이 체결되면 보증금을 반환한다.", "보증금 반환 지연"),
    # 담보 추가 설정 허용 (위험)
    ("임차인은 임대인의 추가 대출에 동의한다.", "담보 추가 설정 허용"),
    ("임대인은 계약 기간 중 근저당을 설정할 수 있으며 임차인은 이의를 제기하지 않는다.", "담보 추가 설정 허용"),
    # 수리비 전가 (주의)
    ("계약 기간 중 발생하는 모든 수리 비용은 임차인이 부담한다.", "수리비 전가"),
    ("보일러 고장 시 세입자가 책임지고 교체한다.", "수리비 전가"),
    ("시설물 수선은 임차인이 처리한다.", "수리비 전가"),
    # 과도한 원상복구 (주의)
    ("퇴실 시 도배·장판 전체를 임차인 비용으로 원상복구한다.", "과도한 원상복구"),
    ("퇴거 시 입주 전 상태로 원상회복한다.", "과도한 원상복구"),
    # 중도 해지 위약금 (주의)
    ("임차인이 중도 퇴실하는 경우 보증금에서 월세 3개월분을 위약금으로 공제한다.", "중도 해지 위약금"),
    ("만기 전 이사하면 위약금 100만 원을 낸다.", "중도 해지 위약금"),
    ("계약 기간 중 해지 시 2달분 월세를 배상한다.", "중도 해지 위약금"),
    # 세입자 보호·생활 규칙 (정상이어야 함 → 오탐 확인)
    ("임대인은 잔금일 다음날까지 근저당 등 담보권을 설정하지 않는다.", "OK"),
    ("임대인이 보증금을 반환하지 못할 경우 임차인은 계약을 해제할 수 있다.", "OK"),
    ("계약 기간 중 시설 고장은 임대인이 수리한다.", "OK"),
    ("임차인의 고의·과실로 인한 고장은 임차인이 부담한다.", "OK"),
    ("전입신고 후 확정일자를 받는다.", "OK"),
    ("통상적인 사용으로 생긴 마모를 제외하고 원상복구한다.", "OK"),
    ("관리비에는 수도·인터넷 요금이 포함된다.", "OK"),
    ("반려동물은 키울 수 없다.", "OK"),
    ("임대인은 대출을 받지 않는다.", "OK"),
]
# 규칙이 못 잡는 것으로 알려진 표현 (한계로 발표에 명시). 위험인데 '확인 필요'로 가면 미탐은 아니지만 판정이 약함.
KNOWN_LIMITS = [
    ("임대인 사정상 주소지는 다른 곳으로 유지해 주세요.", "전입신고 제한"),   # 전입·주민등록 단어 없음
    ("보증금 반환은 임대인 형편에 따라 조정할 수 있다.", "보증금 반환 지연"),  # 새 임차인 언급 없음
    ("임차인은 임대인이 수리하지 않는 경우에도 이의를 제기하지 않는다.", "수리비 전가"),
]


def test_variants():
    """[8] 표현 바꾸기: 위험·주의 유형별 탐지율과 정상 문장 오탐."""
    from collections import Counter
    hit, miss, fp = Counter(), [], []
    for sent, expect in VARIANTS:
        r = cc.classify_special(sent)
        if expect == "OK":
            if r["status"] != cc.OK:
                fp.append((sent, r["type"]))
        elif r["type"] == expect:
            hit[expect] += 1
        else:
            miss.append((sent, expect, r["type"]))
    risky = [v for v in VARIANTS if v[1] != "OK"]
    safe = len(VARIANTS) - len(risky)
    print(f"\n[8] 표현 바꾸기: 위험·주의 {len(risky)}문장 중 {len(risky) - len(miss)}개 탐지, "
          f"정상 {safe}문장 중 오탐 {len(fp)}개")
    for sent, expect in KNOWN_LIMITS:
        r = cc.classify_special(sent)
        print(f"    한계: “{sent}” → {r['status']} ({r['type']}) / 사람 판단: {expect}")
        assert r["status"] != cc.OK, "규칙이 모르는 문장은 최소한 '확인 필요'여야 함"
    assert not miss, miss
    assert not fp, fp


def test_ai_specials(rows, docs):
    """[9] AI 특약 설명: 검사·병합·실패 대체."""
    j = next(r for r in rows if r["case_id"] == "J")
    text = docs[j["id"]]
    base = cc.check(text, j["kind"])
    specials = base["items_by_key"]["special"]["specials"]
    assert all(s["status"] == cc.UNKNOWN and s["question"] for s in specials)  # AI 없이도 질문이 있음

    good = {0: dict(index=0, concern=True, category="계약 연장·변경", quote="다른 호실로 옮길 수 있다",
                    explain="집주인 사정으로 계약 중에 방을 바꿔야 할 수도 있다는 뜻이에요.",
                    question="어떤 경우에 방을 옮기게 되나요? 이사 비용은 누가 내나요?"),
            1: dict(index=1, concern=False, category="계약 연장·변경", quote="",
                    explain="연락이 없으면 계약이 자동으로 1년 늘어난다는 뜻이에요.", question="")}
    res = cc.check(text, j["kind"], ai_items=good)
    sp = res["items_by_key"]["special"]["specials"]
    assert sp[0]["ai"]["quote"] == "다른 호실로 옮길 수 있다" and sp[0]["status"] == cc.UNKNOWN
    assert sp[1]["status"] == cc.UNKNOWN, "AI가 문제없다고 해도 규칙이 모르는 문장은 확인 필요로 남음"

    # 원문에 없는 인용은 지우고, 단정 표현이 있으면 설명 자체를 버림, 범위 밖 번호·형식 오류는 무시
    bad = {0: dict(concern=True, category="없는 분류", quote="보증금을 떼먹는다", explain="이 특약은 무효입니다.", question=""),
           1: dict(concern=True, category="기타", quote="1년 연장", explain="자동으로 연장되는 조항이에요.",
                   question="이 계약은 안전합니다."),
           7: dict(explain="범위 밖"), "x": "형식 오류"}
    res = cc.check(text, j["kind"], ai_items=bad)
    sp = res["items_by_key"]["special"]["specials"]
    assert "ai" not in sp[0], "단정 표현('무효입니다') 설명은 버림"
    assert sp[1]["ai"]["quote"] == "1년 연장" and sp[1]["ai"]["question"] == "" and sp[1]["ai"]["category"] == "기타"

    # AI는 판정을 낮추지 못함: 위험 특약에 concern=False를 줘도 위험 그대로
    c = next(r for r in rows if r["case_id"] == "C")
    res = cc.check(docs[c["id"]], c["kind"], ai_items={0: dict(concern=False, category="생활 규칙", quote="",
                                                               explain="생활 규칙이에요.", question="")})
    assert res["items_by_key"]["special"]["specials"][0]["status"] == cc.RISK
    assert res["overall"]["level"] == cc.RISK
    # AI는 정상 문장을 '확인 필요'로 올릴 수 있음 (후보 찾기)
    res = cc.check(docs[c["id"]], c["kind"], ai_items={1: dict(concern=True, category="생활 규칙", quote="반려동물",
                                                               explain="반려동물을 키우면 위약 사유가 될 수 있어요.",
                                                               question="이미 키우는 경우 어떻게 되나요?")})
    s1 = res["items_by_key"]["special"]["specials"][1]
    assert s1["status"] == cc.UNKNOWN and s1["by"] == "AI" and s1["type"].startswith("AI가 찾은 확인 후보")

    # /api/parse 특약 호출 흉내: 성공 / 한도 / 연결 실패 / 이상한 번호
    ai_parse.os.environ["SAFEHOME_API_URL"] = "https://example.test/api/parse"
    sents = [s["sentence"] for s in specials]
    ok = lambda url, p: (200, json.dumps({"ok": True, "items": [good[0], good[1]]}))
    quota = lambda url, p: (429, json.dumps({"ok": False, "error": "global_minute"}))
    down = lambda url, p: (_ for _ in ()).throw(OSError("down"))
    weird = lambda url, p: (200, json.dumps({"ok": True, "items": [{"index": 9}, {"index": True}, "x"]}))
    items, msg = ai_parse.request_specials(sents, post=ok)
    assert set(items) == {0, 1} and msg is None
    for fake, word in ((quota, "이용자"), (down, "연결"), (weird, "쓸 수 있는 답")):
        items, msg = ai_parse.request_specials(sents, post=fake)
        assert items is None and word in msg and "규칙" in msg, msg
    assert ai_parse.request_specials(["가" * 201], post=ok)[0] is None
    del ai_parse.os.environ["SAFEHOME_API_URL"]
    # 실패하면 화면은 규칙 결과(=AI 없이 check) 그대로
    assert cc.check(text, j["kind"], ai_items=None)["overall"] == base["overall"]
    print("\n[9] AI 특약 설명: 인용·단정 검사, 판정 못 낮춤, 실패 시 규칙 대체 →", msg)


def test_checklist(rows, docs):
    """[10] 직접 확인할 것: 유형·계약서에 따라 바뀌고, 모두 미확인에서 시작."""
    one = next(r for r in rows if r["kind"] == "원룸" and r["case_id"] == "A")
    off = next(r for r in rows if r["kind"] == "오피스텔")
    agent = next(r for r in rows if r["case_id"] == "B")
    h = next(r for r in rows if r["case_id"] == "H")
    ck = {r["id"]: {c["key"]: c for c in cc.check(docs[r["id"]], r["kind"])["checklist"]} for r in (one, off, agent, h)}
    assert all(c["status"] == cc.UNCHECKED for d in ck.values() for c in d.values())
    assert ck[one["id"]]["prior"]["important"] and not ck[off["id"]]["prior"]["important"]
    assert "agent" in ck[agent["id"]] and "agent" not in ck[one["id"]]
    assert ck[h["id"]]["tax"]["important"] and not ck[one["id"]]["tax"]["important"]
    assert all(c["sources"] for c in ck[one["id"]].values() if c["key"] != "site"), "현장 외 항목은 근거가 있어야 함"
    # 계약서에 적힌 선순위 금액이 체크리스트 힌트로 연결
    assert "선순위 보증금" in ck[one["id"]]["prior"]["hint"]
    # 보증금 비율 계산기
    assert cc.deposit_ratio(None, 1, 1, 10) is None
    assert cc.deposit_ratio(45_000_000, 20_000_000, 10_000_000, 100_000_000)["level"] == cc.CAUTION  # 75%
    print("\n[10] 직접 확인할 것 (원룸):", ", ".join(c["title"] for c in ck[one["id"]].values()))


if __name__ == "__main__":
    db = Path(tempfile.mkdtemp()) / "test.db"
    db_seed.build(db)
    rows, docs, cases = load(db)
    test_cases(rows, docs, cases)
    test_missing_info()
    test_weights()
    test_filter_and_sentence(rows)
    test_market(rows, db)
    test_ai_parse(rows)
    test_highlights(rows, docs)
    test_variants()
    test_ai_specials(rows, docs)
    test_checklist(rows, docs)
    print("\n모든 테스트 통과 ✅")
