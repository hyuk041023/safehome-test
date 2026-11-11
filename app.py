"""Safe Home MVP — 충북대 자취방 추천 + 계약 위험 점검 (AI 없는 규칙 기반 버전)

실행: streamlit run app.py
화면: ① 집 찾기  ② 추천 결과  ③ 계약 점검
데이터: safehome.db (가상 매물 100건 · 텍스트 가상 계약서/등기) + market_prices.csv (국토부 실거래가 중위값).
       DB가 없으면 자동 생성.
"""
from pathlib import Path

import streamlit as st

import contract_check as cc
import db_seed
import recommend as rc

DB_PATH = Path(__file__).with_name("safehome.db")
STATUS_ICON = {cc.OK: "🟢", cc.CAUTION: "🟡", cc.RISK: "🔴", cc.UNKNOWN: "⚪"}

st.set_page_config(page_title="Safe Home · 자취방 안심 점검", page_icon="🏠", layout="wide")


# ── 데이터 ────────────────────────────────────────────────────
@st.cache_data
def load_data():
    try:
        import sqlite3
    except ImportError:  # 브라우저(stlite) 환경 등 sqlite3가 없으면 같은 데이터를 메모리에서 생성
        return db_seed.build_memory()
    if not DB_PATH.exists():
        db_seed.build(DB_PATH)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("SELECT * FROM listings ORDER BY id")]
    contracts = {r["listing_id"]: r["contract_text"] for r in con.execute("SELECT * FROM contracts")}
    registries = {r["listing_id"]: r["registry_text"] for r in con.execute("SELECT * FROM registries")}
    market = {(r["dong"], r["kind"]): (r["median_monthly"], r["sample_n"])
              for r in con.execute("SELECT * FROM market_prices")}
    con.close()
    return rows, contracts, registries, market


rows, contracts, registries, market = load_data()
by_id = {r["id"]: r for r in rows}
_real = rc.load_market_csv(Path(__file__).with_name("market_prices.csv"))
MARKET_SOURCE = (_real[1] if _real and _real[1] else "국토교통부 실거래가") if _real else "샘플 시세"
MARKET_TAG = "국토부 실거래가" if _real else "샘플 시세"

# ── 입력 상태 기본값 ──────────────────────────────────────────
DEFAULTS = dict(f_rent=45, f_dep="상관없음", f_gate="정문", f_walk=15,
                f_kinds=list(rc.KINDS), f_opts=[], f_pri=[], f_top=5,
                sentence="충북대 정문 걸어서 10분 안, 월세 40만 원 이하, 조용하고 세탁기 있는 곳",
                sel_listing=rows[0]["id"])
for k, v in DEFAULTS.items():
    st.session_state.setdefault(k, v)
DEPOSIT_CHOICES = {"300만 원": 3_000_000, "500만 원": 5_000_000, "1,000만 원": 10_000_000,
                   "1,500만 원": 15_000_000, "2,000만 원": 20_000_000, "상관없음": None}


def apply_sentence():
    """문장을 규칙으로 해석해 입력칸을 채운다. 읽지 못한 값은 기존 값을 그대로 둔다."""
    cond, understood = rc.parse_sentence(st.session_state.sentence)
    if "max_rent" in cond:
        st.session_state.f_rent = cond["max_rent"] // 10_000
    if "max_deposit" in cond:
        target = cond["max_deposit"]
        fit = [k for k, v in DEPOSIT_CHOICES.items() if v and v >= target]
        st.session_state.f_dep = fit[0] if fit else "상관없음"
    if "gate" in cond:
        st.session_state.f_gate = cond["gate"]
    if "max_walk" in cond:
        st.session_state.f_walk = min(30, max(5, cond["max_walk"]))
    if "kinds" in cond:
        st.session_state.f_kinds = cond["kinds"]
    if "options" in cond:
        st.session_state.f_opts = cond["options"]
    if "priorities" in cond:
        st.session_state.f_pri = cond["priorities"]
    st.session_state.understood = understood


def pick_listing(listing_id):
    st.session_state.sel_listing = listing_id


def current_condition():
    return dict(max_rent=st.session_state.f_rent * 10_000,
                max_deposit=DEPOSIT_CHOICES[st.session_state.f_dep],
                gate=st.session_state.f_gate, max_walk=st.session_state.f_walk,
                kinds=st.session_state.f_kinds, options=st.session_state.f_opts,
                priorities=st.session_state.f_pri)


def condition_tags(c):
    tags = [f"월세 {c['max_rent'] // 10_000}만 원 이하",
            f"보증금 {st.session_state.f_dep} 이하" if c["max_deposit"] else "보증금 상관없음",
            f"{c['gate']} 도보 {c['max_walk']}분 이내"]
    if c["kinds"] and len(c["kinds"]) < len(rc.KINDS):
        tags.append("유형: " + ", ".join(c["kinds"]))
    if c["options"]:
        tags.append("필수: " + ", ".join(c["options"]))
    tags.append("우선순위: " + (" > ".join(c["priorities"]) if c["priorities"] else "설문 기본값"))
    return tags


@st.cache_data
def check_listing(listing_id, contract_text, registry_text):
    return cc.check(contract_text, registry_text)


# ── 헤더 ──────────────────────────────────────────────────────
st.title("🏠 Safe Home")
st.caption("충북대 근처 자취방을 찾고, 계약 전에 위험 신호를 같이 확인해요.")
st.info("시연용 MVP예요. 매물·계약서·등기·시세는 모두 **가상 샘플 데이터**이고, 결과는 참고용이에요. "
        "계약의 안전을 보장하지 않으며 법률 자문이 아니에요.", icon="ℹ️")

with st.sidebar:
    st.header("이 화면의 기준")
    st.markdown("**설문 기본 가중치** (103명, 복수 선택)")
    st.dataframe([{"항목": k, "가중치": v} for k, v in rc.BASE_WEIGHTS.items()], hide_index=True,
                 use_container_width=True)
    st.markdown(f"- 보증금 환산율 {rc.CONVERSION_RATE:.0%} (가정값)\n"
                f"- 시세 ±{rc.MARKET_BAND:.0%} 이내면 '비슷'\n"
                f"- 시세 출처: {MARKET_SOURCE}. 거래 {rc.MIN_SAMPLES}건 미만인 동은 {rc.AREA_NAME} 전체 기준\n"
                f"- 보증금 회수 위험 비율 {cc.RATIO_CAUTION:.0%} 주의 · {cc.RATIO_RISK:.0%} 위험")
    st.caption("판정은 모두 규칙으로 계산해요. 같은 입력이면 항상 같은 결과가 나와요.")

tab1, tab2, tab3 = st.tabs(["① 집 찾기", "② 추천 결과", "③ 계약 점검"])

# ── ① 집 찾기 ─────────────────────────────────────────────────
with tab1:
    st.subheader("어떤 집을 찾고 있나요?")
    st.text_input("문장으로 말해 주세요", key="sentence")
    st.button("문장으로 조건 채우기", on_click=apply_sentence, type="primary")
    if st.session_state.get("understood") is not None:
        u = st.session_state.understood
        if u:
            st.success("이해한 조건: " + " · ".join(u))
        else:
            st.warning("문장에서 조건을 찾지 못했어요. 아래 입력칸에서 직접 골라 주세요.")
    st.caption("문장에서 읽지 못한 조건은 추측하지 않아요. 아래 입력칸에서 언제든 직접 고칠 수 있어요.")

    st.divider()
    c1, c2, c3 = st.columns(3)
    with c1:
        st.number_input("월세 상한 (만 원)", min_value=10, max_value=150, step=1, key="f_rent")
        st.selectbox("보증금 상한", list(DEPOSIT_CHOICES), key="f_dep")
    with c2:
        st.selectbox("기준 문", list(rc.GATES), key="f_gate")
        st.slider("도보 시간 (분 이내)", 5, 30, key="f_walk")
    with c3:
        st.multiselect("집 유형", rc.KINDS, key="f_kinds")
        st.multiselect("꼭 있어야 하는 옵션", rc.OPTIONS, key="f_opts")
    st.multiselect("중요하게 보는 것 (고른 순서가 우선순위)", rc.FACTORS, key="f_pri",
                   help="고르지 않으면 설문 기본 가중치를 써요. 고르면 설문값 50% + 고른 순서 50%로 섞어요.")
    st.radio("추천 개수", [3, 5], horizontal=True, key="f_top")
    st.caption("입력하면 바로 ② 추천 결과에 반영돼요.")

# ── ② 추천 결과 ───────────────────────────────────────────────
with tab2:
    cond = current_condition()
    weights = rc.blend_weights(cond["priorities"])
    st.markdown("**지금 적용된 조건** — 틀린 곳은 ① 집 찾기에서 고치면 바로 다시 찾아요.")
    st.markdown(" ".join(f"`{t}`" for t in condition_tags(cond)))
    if "조용함" not in (cond["priorities"] or []) and "조용" in (st.session_state.sentence or ""):
        st.caption("※ '조용함'은 우선순위에 넣었을 때만 점수에 반영돼요.")

    hits = rc.filter_listings(rows, cond)
    ranked = rc.score_listings(hits, weights, cond["gate"])[: st.session_state.f_top]
    st.write(f"조건을 모두 만족하는 매물 **{len(hits)}건** 중 상위 {len(ranked)}건")
    if not ranked:
        st.warning("조건에 맞는 매물이 없어요. 월세 상한이나 도보 시간을 조금 늘려 보세요.")

    for rank, item in enumerate(ranked, 1):
        r = item["row"]
        res = check_listing(r["id"], contracts[r["id"]], registries[r["id"]])
        flags = cc.count_flags(res)
        mk = rc.market_compare(r, market)
        reasons, weak = rc.explain(item, weights, cond["gate"])
        with st.container(border=True):
            h1, h2 = st.columns([3, 1])
            with h1:
                st.markdown(f"### {rank}. {r['name']}")
                st.caption(f"{r['address']} · {r['kind']} · {r['area']}㎡ · 옵션: {r['options'] or '없음'}")
            with h2:
                st.metric("점수", f"{item['score']:.2f}")
            m1, m2, m3 = st.columns(3)
            m1.metric("진짜 한 달 비용", f"{rc.real_monthly(r) / 10_000:.0f}만 원",
                      help="월세 + 관리비")
            m1.caption(f"월세 {r['rent'] // 10_000}만 + 관리비 {r['maint'] // 10_000}만 · 보증금 {r['deposit'] // 10_000:,}만")
            m2.metric("주변 시세 대비", mk["label"])
            m2.caption(mk["detail"] + f" · {MARKET_TAG}")
            badge = []
            if flags[cc.RISK]:
                badge.append(f"🔴 위험 {flags[cc.RISK]}")
            if flags[cc.CAUTION]:
                badge.append(f"🟡 주의 {flags[cc.CAUTION]}")
            if flags[cc.UNKNOWN]:
                badge.append(f"⚪ 확인 필요 {flags[cc.UNKNOWN]}")
            m3.metric("계약 전 확인할 것", f"{sum(flags.values())}개")
            m3.caption(" · ".join(badge) if badge else "🟢 큰 위험 신호 없음 (참고용)")
            st.markdown("**추천 이유** · " + " / ".join(reasons))
            if weak:
                st.markdown("**다른 후보보다 아쉬운 점** · " + " / ".join(weak))
            with st.expander("점수 내역 보기"):
                st.dataframe([{"항목": f, "가중치": round(weights[f], 3), "점수": round(item["parts"][f], 3)}
                              for f in rc.FACTORS if weights[f] > 0], hide_index=True, use_container_width=True)
            st.button("이 집 계약 점검하기 → ③ 탭에서 확인", key=f"pick_{r['id']}",
                      on_click=pick_listing, args=(r["id"],))

# ── ③ 계약 점검 ───────────────────────────────────────────────
with tab3:
    st.subheader("계약 전 위험 신호 점검")
    ids = [r["id"] for r in rows]
    st.selectbox("점검할 매물", ids, key="sel_listing",
                 format_func=lambda i: f"{by_id[i]['name']} (예시 사례 {by_id[i]['case_id']})")
    lid = st.session_state.sel_listing
    st.caption("예시 계약서와 등기 요약을 불러왔어요. 내용을 고치면 바로 다시 점검해요. "
               "실제 계약서·개인정보는 입력하지 마세요.")
    t1, t2 = st.columns(2)
    with t1:
        contract_text = st.text_area("📄 가상 계약서 (텍스트)", contracts[lid], height=420, key=f"ct_{lid}")
    with t2:
        registry_text = st.text_area("📑 가상 등기사항 요약 (텍스트)", registries[lid], height=420, key=f"rg_{lid}")

    res = cc.check(contract_text, registry_text)
    ov = res["overall"]
    msg = f"종합: **{ov['label']}** — 규칙 기반 참고 결과예요."
    {cc.RISK: st.error, cc.CAUTION: st.warning, cc.UNKNOWN: st.info, cc.OK: st.success}[ov["level"]](msg)
    if ov["level"] == cc.UNKNOWN:
        st.caption("확인되지 않은 항목이 있어 '양호'로 판단하지 않았어요.")

    st.markdown("#### 항목별 결과")
    for it in res["items"]:
        with st.container(border=True):
            st.markdown(f"{STATUS_ICON[it['status']]} **{it['name']}** — {it['status']}")
            st.write(it["detail"])
            if it["evidence"]:
                st.caption(f"근거 문구: “{it['evidence']}”")
            if it["key"] == "special" and it.get("specials"):
                for s in it["specials"]:
                    st.markdown(f"- {STATUS_ICON[s['status']]} {s['sentence']}  \n  → *{s['type']}*: {s['why']}")

    st.markdown("#### 집주인·중개사에게 물어볼 것")
    if res["questions"]:
        for q in res["questions"]:
            st.markdown(f"- {q}")
    else:
        st.write("특별히 물어볼 항목이 없어요. 계약 당일 최신 등기부등본은 한 번 더 확인하세요.")
    st.text_area("보낼 문자 초안 (복사해서 쓰세요)", cc.message_draft(by_id[lid]["name"], res["questions"]),
                 height=220, key=f"msg_{lid}_{hash(contract_text + registry_text)}")

    with st.expander("문서에서 읽은 값 보기 (규칙이 무엇을 읽었는지 확인)"):
        c, g = res["contract"], res["registry"]
        st.dataframe([
            {"항목": "임대인", "읽은 값": c["lessor"]}, {"항목": "대리인", "읽은 값": c["agent"]},
            {"항목": "보증금", "읽은 값": c["deposit"]}, {"항목": "월세", "읽은 값": c["rent"]},
            {"항목": "관리비", "읽은 값": c["maint"]}, {"항목": "건물 추정 시세", "읽은 값": c["value"]},
            {"항목": "선순위 보증금", "읽은 값": c["prior"]}, {"항목": "납세증명서", "읽은 값": c["tax"]},
            {"항목": "등기 소유자", "읽은 값": g["owner"]}, {"항목": "소유권 이전일", "읽은 값": g["transfer"]},
            {"항목": "근저당 채권최고액", "읽은 값": g["mortgage"]}, {"항목": "압류", "읽은 값": g["seizure"]},
            {"항목": "가압류", "읽은 값": g["provisional"]}, {"항목": "신탁", "읽은 값": g["trust"]},
            {"항목": "임차권등기", "읽은 값": g["lease_reg"]},
        ], hide_index=True, use_container_width=True)
        st.caption("값이 비어 있으면(None) 문서에서 찾지 못한 것이고, 해당 항목은 '확인 필요'로 처리돼요.")
