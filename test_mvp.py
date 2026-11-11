"""MVP 검증 테스트. 실행: python test_mvp.py

1) 계약 사례 10종이 모든 매물에서 기대한 종합 판정으로 나오는지
2) 정보가 빠진 경우 추측하지 않고 '확인 필요'로 가는지
3) 설문 가중치 · 우선순위 혼합값이 노션(11/10 회의록) 예시와 같은지
4) 추천 결과가 필수 조건을 100% 만족하는지 (문제 정의 성공 기준)
"""
import sqlite3
import tempfile
from pathlib import Path

import db_seed
import contract_check as cc
import recommend as rc


def load(db):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("SELECT * FROM listings")]
    docs = {r["listing_id"]: r["contract_text"] for r in con.execute("SELECT * FROM contracts")}
    regs = {r["listing_id"]: r["registry_text"] for r in con.execute("SELECT * FROM registries")}
    cases = {r["case_id"]: dict(r) for r in con.execute("SELECT * FROM cases")}
    return rows, docs, regs, cases


def test_cases(rows, docs, regs, cases):
    table, fails = {}, []
    for r in rows:
        res = cc.check(docs[r["id"]], regs[r["id"]])
        exp = cases[r["case_id"]]["expected"]
        table.setdefault(r["case_id"], set()).add(res["overall"]["label"])
        if res["overall"]["label"] != exp:
            fails.append((r["id"], r["case_id"], exp, res["overall"]["label"],
                          [(i["name"], i["status"]) for i in res["items"] if i["status"] != cc.OK]))
    print("\n[1] 사례별 기대 판정")
    for k in sorted(cases):
        ok = table[k] == {cases[k]["expected"]}
        print(f"  {'✅' if ok else '❌'} {k} {cases[k]['title']:<22} 기대: {cases[k]['expected']:<10} 실제: {', '.join(table[k])}")
    for f in fails:
        print("   실패:", f)
    assert not fails


def test_missing_info():
    res = cc.check("임대인: 김가상\n보증금: 금 5,000,000원", "소유자: 김가상 (2020.01.01 소유권이전)")
    sts = {i["key"]: i["status"] for i in res["items"]}
    print("\n[2] 정보 부족 문서:", res["overall"]["label"], sts)
    assert res["overall"]["label"] == "확인 후 판단"
    assert sts["ratio"] == sts["seizure"] == sts["tax"] == sts["special"] == cc.UNKNOWN
    assert sts["owner"] == cc.OK
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


if __name__ == "__main__":
    db = Path(tempfile.mkdtemp()) / "test.db"
    db_seed.build(db)
    rows, docs, regs, cases = load(db)
    test_cases(rows, docs, regs, cases)
    test_missing_info()
    test_weights()
    test_filter_and_sentence(rows)
    print("\n모든 테스트 통과 ✅")
