"""Safe Home MVP — 충북대 자취방 추천 + 계약 위험 점검

실행: streamlit run app.py
화면: 1 집 찾기 → 2 추천 결과 → 3 계약 점검 (버튼을 누르면 다음 단계로 자동 이동)
데이터: safehome.db (가상 매물 100건 · 텍스트 가상 계약서) + market_prices.csv (국토부 실거래가 중위값).
       DB가 없으면 자동 생성.
문장 해석: 「찾기」는 문장을 Gemini(/api/parse 서버리스 함수)로 조건으로 바꾼 뒤 바로 검색해요. 실패하면 규칙 해석.
          「이 조건으로 찾기」는 입력칸 값으로만 검색해요(Gemini 0회). 검색·점수는 규칙이에요.
계약 점검(11/13): 가상 계약서만 규칙으로 분석하고, 등기·건축물대장·세금·현장은 직접 확인할 체크리스트(미확인)로
          따로 보여 줘요. 「AI로 특약 설명 듣기」를 누를 때만 Gemini가 특약 설명·질문을 만들어요(실패하면 규칙 결과).
"""
import copy
import html
from pathlib import Path

import streamlit as st

import ai_parse
import contract_check as cc
import db_seed
import recommend as rc

DB_PATH = Path(__file__).with_name("safehome.db")

st.set_page_config(page_title="Safe Home", page_icon="🏠", layout="wide",
                   initial_sidebar_state="collapsed")

# 상태별 색 (밝은·어두운 화면 모두에서 읽히도록 반투명 배경)
TONE = {
    cc.RISK: ("위험", "#e5484d", "rgba(229,72,77,0.14)"),
    cc.CAUTION: ("주의", "#d97706", "rgba(217,119,6,0.14)"),
    cc.UNKNOWN: ("확인 필요", "#8b8d98", "rgba(139,141,152,0.16)"),
    cc.OK: ("정상", "#16a34a", "rgba(22,163,74,0.12)"),
}

st.markdown("""
<style>
[data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"] { display: none; }
.block-container { padding-top: 2rem; max-width: 1100px; }
.sh-chip { display: inline-block; padding: 2px 10px; margin: 0 6px 6px 0; border-radius: 999px;
           font-size: 0.85rem; border: 1px solid rgba(128,128,128,0.35); }
.sh-badge { display: inline-block; padding: 1px 8px; margin-right: 6px; border-radius: 6px;
            font-size: 0.8rem; font-weight: 600; }
.sh-doc { border: 1px solid rgba(128,128,128,0.35); border-radius: 10px; padding: 14px 16px;
          font-size: 0.9rem; line-height: 1.75; }
.sh-doc .ln { padding: 1px 8px; margin: 0 -8px; border-left: 3px solid transparent; white-space: pre-wrap; }
.sh-doc .hd { font-weight: 700; margin-top: 6px; }
.sh-doc .ttl { font-weight: 700; font-size: 1rem; margin-bottom: 4px; }
.sh-no { display: inline-block; min-width: 1.35em; height: 1.35em; line-height: 1.35em; margin-left: 6px;
         border-radius: 999px; text-align: center; font-size: 0.75rem; font-weight: 700; color: #fff; }
.sh-verdict { border-radius: 10px; padding: 12px 16px; margin: 4px 0 16px; font-weight: 600; }
.sh-price { font-size: 1.6rem; font-weight: 700; line-height: 1.2; }
.sh-muted { opacity: 0.7; font-size: 0.85rem; }
</style>
""", unsafe_allow_html=True)


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
    try:
        rows = [dict(r) for r in con.execute("SELECT * FROM listings ORDER BY id")]
        con.execute("SELECT expected FROM cases LIMIT 1")
        if con.execute("SELECT name FROM sqlite_master WHERE name='registries'").fetchone():
            raise sqlite3.OperationalError("old db")  # 11/13 이전 DB면 새로 만든다
    except sqlite3.OperationalError:
        con.close()
        db_seed.build(DB_PATH)
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute("SELECT * FROM listings ORDER BY id")]
    contracts = {r["listing_id"]: r["contract_text"] for r in con.execute("SELECT * FROM contracts")}
    market = {(r["dong"], r["kind"]): (r["median_monthly"], r["sample_n"])
              for r in con.execute("SELECT * FROM market_prices")}
    con.close()
    return rows, contracts, market


rows, contracts, market = load_data()
by_id = {r["id"]: r for r in rows}

# ── 상태 ──────────────────────────────────────────────────────
DEFAULTS = dict(step=1, f_rent=45, f_dep="상관없음", f_gate="정문", f_walk=15,
                f_kinds=list(rc.KINDS), f_opts=[], f_pri=[], f_top=5,
                sentence="", sel_listing=rows[0]["id"], ai_calls=0, searched=None,
                understood=None, scroll_top=False)
for k, v in DEFAULTS.items():
    st.session_state.setdefault(k, v)
# 다른 단계로 가서 입력칸이 화면에 없으면 Streamlit이 값을 지우므로, 매번 다시 넣어 유지한다
for k in ("f_rent", "f_dep", "f_gate", "f_walk", "f_kinds", "f_opts", "f_pri", "f_top", "sentence", "sel_listing"):
    st.session_state[k] = st.session_state[k]
DEPOSIT_CHOICES = {"300만 원": 3_000_000, "500만 원": 5_000_000, "1,000만 원": 10_000_000,
                   "1,500만 원": 15_000_000, "2,000만 원": 20_000_000, "상관없음": None}
STEPS = {1: "1  집 찾기", 2: "2  추천 결과", 3: "3  계약 점검"}


def go(step):
    st.session_state.step = step
    st.session_state.scroll_top = True


def current_condition():
    return dict(max_rent=st.session_state.f_rent * 10_000,
                max_deposit=DEPOSIT_CHOICES[st.session_state.f_dep],
                gate=st.session_state.f_gate, max_walk=st.session_state.f_walk,
                kinds=list(st.session_state.f_kinds), options=list(st.session_state.f_opts),
                priorities=list(st.session_state.f_pri))


def fill_inputs(cond):
    """해석한 조건으로 입력칸을 채운다. 읽지 못한 값은 기존 값을 그대로 둔다."""
    if "max_rent" in cond:
        st.session_state.f_rent = max(10, min(150, cond["max_rent"] // 10_000))
    if "max_deposit" in cond:
        fit = [k for k, v in DEPOSIT_CHOICES.items() if v and v >= cond["max_deposit"]]
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


def save_search():
    st.session_state.searched = dict(cond=copy.deepcopy(current_condition()), top=st.session_state.f_top,
                                     dep_label=st.session_state.f_dep)


def find_by_sentence():
    """「찾기」: 문장 → 조건(Gemini 1회, 실패하면 규칙) → 검색 → 2단계로."""
    sentence = (st.session_state.sentence or "").strip()
    if sentence:
        fields = None
        if st.session_state.ai_calls < ai_parse.SESSION_LIMIT:
            st.session_state.ai_calls += 1
            fields, _ = ai_parse.request_parse(sentence)
        cond, understood = rc.cond_from_ai(fields) if fields is not None else rc.parse_sentence(sentence)
        fill_inputs(cond)
        st.session_state.understood = understood
    save_search()
    go(2)


def find_by_inputs():
    """「이 조건으로 찾기」: 입력칸 값으로만 검색 (Gemini 0회) → 2단계로."""
    st.session_state.understood = None
    save_search()
    go(2)


def explain_specials(ai_key, sentences):
    """「AI로 특약 설명 듣기」: Gemini 1회. 실패해도 규칙 결과는 그대로 보여 준다."""
    st.session_state.ai_calls += 1
    items, message = ai_parse.request_specials(sentences)
    st.session_state[ai_key] = dict(items=items, message=message)


def open_contract(listing_id):
    st.session_state.sel_listing = listing_id
    go(3)


if st.session_state.searched is None:
    save_search()


def results():
    s = st.session_state.searched
    weights = rc.blend_weights(s["cond"]["priorities"])
    hits = rc.filter_listings(rows, s["cond"])
    return hits, rc.score_listings(hits, weights, s["cond"]["gate"])[: s["top"]], weights


def condition_tags(c, dep_label):
    tags = [f"월세 {c['max_rent'] // 10_000}만 원 이하",
            f"보증금 {dep_label} 이하" if c["max_deposit"] else "보증금 상관없음",
            f"{c['gate']} 도보 {c['max_walk']}분"]
    if c["kinds"] and len(c["kinds"]) < len(rc.KINDS):
        tags.append(", ".join(c["kinds"]))
    if c["options"]:
        tags.append(", ".join(c["options"]) + " 필수")
    if c["priorities"]:
        tags.append(" > ".join(c["priorities"]) + " 우선")
    return tags


def chips(tags):
    return "".join(f'<span class="sh-chip">{html.escape(t)}</span>' for t in tags)


def badge(status, text=None):
    label, color, bg = TONE[status]
    return f'<span class="sh-badge" style="color:{color};background:{bg}">{html.escape(text or label)}</span>'


@st.cache_data
def check_listing(contract_text, kind):
    return cc.check(contract_text, kind)


def links(sources, prefix="근거"):
    if not sources:
        return ""
    return (f'<div class="sh-muted">{prefix}: '
            + " · ".join(f'<a href="{html.escape(u)}" target="_blank">{html.escape(t)}</a>' for t, u in sources)
            + "</div>")


def doc_html(text, doc_marks, note_status):
    """계약서 텍스트를 문서처럼 그리고, 위험·주의·확인 필요 줄에 색과 번호를 붙인다."""
    out = []
    for i, line in enumerate(text.splitlines()):
        s = line.strip()
        nos = cc.line_marks(line, doc_marks)
        cls = "ln ttl" if i == 0 else "ln hd" if s.startswith("[") else "ln"
        body = html.escape(line) if s else "&nbsp;"
        if nos:
            worst = max((note_status[n] for n in nos), key=cc.SEVERITY.get)
            _, color, bg = TONE[worst]
            tags = "".join(f'<span class="sh-no" style="background:{TONE[note_status[n]][1]}">{n}</span>'
                           for n in nos)
            out.append(f'<div class="{cls}" style="background:{bg};border-left-color:{color}">{body}{tags}</div>')
        else:
            out.append(f'<div class="{cls}">{body}</div>')
    return '<div class="sh-doc">' + "".join(out) + "</div>"


# ── 헤더 · 단계 표시 ──────────────────────────────────────────
st.markdown("## Safe Home")
st.caption("충북대 근처 자취방 찾기와 계약 전 점검")
st.radio("단계", list(STEPS), key="step", format_func=STEPS.get, horizontal=True, label_visibility="collapsed")
st.divider()

if st.session_state.scroll_top:  # 단계가 바뀌면 화면 맨 위로
    st.session_state.scroll_top = False
    try:
        import streamlit.components.v1 as components
        components.html("<script>const d=window.parent.document;"
                        "(d.querySelector('[data-testid=\"stMain\"]')||d.querySelector('section.main')||d.body)"
                        ".scrollTo({top:0});window.parent.scrollTo(0,0);</script>", height=0)
    except Exception:
        pass

step = st.session_state.step

# ── 1 집 찾기 ─────────────────────────────────────────────────
if step == 1:
    st.markdown("### 어떤 집을 찾으세요?")
    q1, q2 = st.columns([5, 1], vertical_alignment="bottom")
    with q1:
        st.text_input("찾는 집", key="sentence", max_chars=ai_parse.MAX_CHARS, label_visibility="collapsed",
                      placeholder="예) 정문 걸어서 10분, 월세 40 이하, 조용하고 세탁기 있는 원룸")
    with q2:
        st.button("찾기", type="primary", on_click=find_by_sentence, use_container_width=True)

    with st.expander("조건 직접 고르기"):
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
        st.multiselect("중요하게 보는 것 (고른 순서대로)", rc.FACTORS, key="f_pri")
        st.radio("보여 줄 개수", [3, 5], horizontal=True, key="f_top")
        st.button("이 조건으로 찾기", on_click=find_by_inputs)

# ── 2 추천 결과 ───────────────────────────────────────────────
elif step == 2:
    s = st.session_state.searched
    hits, ranked, weights = results()
    top, side = st.columns([5, 1], vertical_alignment="center")
    with top:
        st.markdown(chips(condition_tags(s["cond"], s["dep_label"])), unsafe_allow_html=True)
    with side:
        st.button("조건 바꾸기", on_click=go, args=(1,), use_container_width=True)
    if st.session_state.understood == []:
        st.caption("문장에서 조건을 찾지 못해 기본 조건으로 찾았어요. 「조건 바꾸기」에서 직접 골라 주세요.")

    st.markdown(f"### 조건에 맞는 집 {len(hits)}곳")
    if not ranked:
        st.info("조건에 맞는 집이 없어요. 월세 상한이나 도보 시간을 조금 늘려 보세요.")

    for rank, item in enumerate(ranked, 1):
        r = item["row"]
        res = check_listing(contracts[r["id"]], r["kind"])
        flags = cc.count_flags(res)
        mk = rc.market_compare(r, market)
        reasons, weak = rc.explain(item, weights, s["cond"]["gate"])
        with st.container(border=True):
            a, b = st.columns([3, 1])
            with a:
                st.markdown(f"#### {rank}. {r['name']}")
                st.markdown(f'<div class="sh-muted">{html.escape(r["address"])} · {r["kind"]} · {r["area"]}㎡'
                            f' · {s["cond"]["gate"]} 도보 {r[rc.GATES[s["cond"]["gate"]]]}분</div>',
                            unsafe_allow_html=True)
                st.markdown(f'<div class="sh-muted">옵션: {html.escape(r["options"] or "없음")}</div>',
                            unsafe_allow_html=True)
            with b:
                st.markdown(f'<div class="sh-muted">한 달 비용</div>'
                            f'<div class="sh-price">{rc.real_monthly(r) / 10_000:.0f}만 원</div>'
                            f'<div class="sh-muted">월세 {r["rent"] // 10_000} + 관리비 {r["maint"] // 10_000}'
                            f' · 보증금 {r["deposit"] // 10_000:,}만</div>', unsafe_allow_html=True)

            tone = {"낮음": cc.OK, "비슷": cc.UNKNOWN, "높음": cc.CAUTION}.get(mk["label"], cc.UNKNOWN)
            line = [badge(tone, f"시세 {mk['label']}")]
            if flags[cc.RISK]:
                line.append(badge(cc.RISK, f"계약서 위험 문구 {flags[cc.RISK]}"))
            if flags[cc.CAUTION]:
                line.append(badge(cc.CAUTION, f"주의 문구 {flags[cc.CAUTION]}"))
            if flags[cc.UNKNOWN]:
                line.append(badge(cc.UNKNOWN, f"확인 필요 {flags[cc.UNKNOWN]}"))
            if not any(flags.values()):
                line.append(badge(cc.OK, "계약서 위험 문구 없음"))
            if mk["diff"] is not None:
                line.append(f'<span class="sh-muted">주변 {r["kind"]} 실거래 시세 대비 {mk["diff"]:+.0%}</span>')
            st.markdown("".join(line), unsafe_allow_html=True)
            st.markdown(f'<div style="margin-top:8px">{html.escape(" · ".join(reasons))}</div>',
                        unsafe_allow_html=True)
            st.button("계약서 점검하기", key=f"pick_{r['id']}", on_click=open_contract, args=(r["id"],),
                      type="primary" if flags[cc.RISK] else "secondary")

# ── 3 계약 점검 ───────────────────────────────────────────────
else:
    _, ranked, _ = results()
    ids = [it["row"]["id"] for it in ranked]
    if st.session_state.sel_listing not in ids:
        ids.insert(0, st.session_state.sel_listing)
    nav, pick = st.columns([1, 4], vertical_alignment="bottom")
    with nav:
        st.button("← 추천 목록", on_click=go, args=(2,), use_container_width=True)
    with pick:
        st.selectbox("점검할 집", ids, key="sel_listing", format_func=lambda i: by_id[i]["name"],
                     label_visibility="collapsed")
    lid = st.session_state.sel_listing
    row = by_id[lid]
    contract_text = st.session_state.get(f"ct_{lid}", contracts[lid])
    ai_key = f"ai_{lid}_{hash(contract_text)}"
    ai_state = st.session_state.get(ai_key)  # dict(items=..., message=...) 또는 None

    res = cc.check(contract_text, row["kind"], ai_items=(ai_state or {}).get("items"))
    hl = cc.highlights(res)
    note_status = {n["no"]: n["status"] for n in hl["notes"]}
    flags = cc.count_flags(res)
    todo = [ck for ck in res["checklist"] if not st.session_state.get(f"ck_{lid}_{ck['key']}")]

    st.markdown(f"### {row['name']}")
    st.markdown(f'<div class="sh-muted">{row["kind"]} · 보증금 {row["deposit"] // 10_000:,}만 · 월세 {row["rent"] // 10_000}만'
                f' · 관리비 {row["maint"] // 10_000}만 · {html.escape(row["address"])}</div>',
                unsafe_allow_html=True)
    ov = res["overall"]
    _, color, bg = TONE[ov["level"]]
    summary = [f"{lbl} {flags[k]}곳" for k, lbl in ((cc.RISK, "위험"), (cc.CAUTION, "주의"), (cc.UNKNOWN, "확인 필요"))
               if flags[k]]
    st.markdown(f'<div class="sh-verdict" style="background:{bg};border-left:4px solid {color}">'
                f'{html.escape(ov["label"])}{" · " + " · ".join(summary) if summary else ""}'
                f'<div class="sh-muted" style="font-weight:400;margin-top:4px">계약서 문구만 본 결과예요. '
                f'아래 서류·현장 {len(todo)}개는 아직 미확인이라, 이 결과가 계약 전체의 안전을 보장하지 않아요.</div></div>',
                unsafe_allow_html=True)

    tab_contract, tab_check = st.tabs(["계약서 분석", f"직접 확인할 것 ({len(todo)}개 미확인)"])

    with tab_contract:
        d1, d2 = st.columns([1, 1])
        with d1:
            st.markdown("**가상 계약서**")
            st.markdown(doc_html(contract_text, hl["marks"], note_status), unsafe_allow_html=True)
        with d2:
            specials = res["items_by_key"]["special"].get("specials", [])
            st.markdown("**표시된 부분**")
            if specials:
                done = ai_state is not None
                st.button("AI로 특약 설명 듣기" if not done else "AI 설명을 받았어요", key=f"aibtn_{ai_key}",
                          disabled=done or st.session_state.ai_calls >= ai_parse.SESSION_LIMIT,
                          on_click=explain_specials, args=(ai_key, [s["sentence"] for s in specials]))
                if ai_state and ai_state.get("message"):
                    st.caption(ai_state["message"])
                elif not ai_state:
                    st.caption("Gemini가 특약을 쉬운 말로 풀어 주고 물어볼 질문을 만들어요. 판정은 바꾸지 않아요.")
            if not hl["notes"]:
                st.markdown("계약서에서 위험·주의·확인 필요로 표시할 문구가 없어요. 오른쪽 탭의 서류·현장은 따로 확인하세요.")
            for n in hl["notes"]:
                label, color, bg = TONE[n["status"]]
                with st.container(border=True):
                    st.markdown(f'<span class="sh-no" style="background:{color};margin-left:0">{n["no"]}</span> '
                                f'{badge(n["status"])}<b>{html.escape(n["title"])}</b>', unsafe_allow_html=True)
                    if n.get("sentence"):
                        st.markdown(f'<div class="sh-muted">원문: “{html.escape(n["sentence"])}”</div>',
                                    unsafe_allow_html=True)
                    st.markdown(html.escape(n["detail"]))
                    ai = n.get("ai")
                    if ai and n["by"] != "AI":
                        quote = f' (“{html.escape(ai["quote"])}”)' if ai["quote"] else ""
                        st.markdown(f'<div style="margin-top:4px"><b>AI 설명</b>{quote}: {html.escape(ai["explain"])}</div>',
                                    unsafe_allow_html=True)
                    if n["question"]:
                        st.markdown(f'<div class="sh-muted">물어볼 것: {html.escape(n["question"])}</div>',
                                    unsafe_allow_html=True)
                    if ai and ai["question"] and ai["question"] != n["question"]:
                        st.markdown(f'<div class="sh-muted">AI가 제안한 질문: {html.escape(ai["question"])}</div>',
                                    unsafe_allow_html=True)
                    st.markdown(links(n["sources"]), unsafe_allow_html=True)
            ok_specials = [s for s in specials if s["status"] == cc.OK]
            if ok_specials:
                with st.expander(f"문제로 표시하지 않은 특약 {len(ok_specials)}개"):
                    for s in ok_specials:
                        extra = f" · AI: {s['ai']['explain']}" if s.get("ai") else ""
                        st.markdown(f"- “{s['sentence']}” — {s['type']}. {s['why']}{extra}")
            st.caption("AI 설명은 참고용이에요. 판정은 규칙으로 하고, AI는 '정상'을 '확인 필요'로 올리기만 할 수 있어요. "
                       "법률 자문이 아니에요.")

    with tab_check:
        st.caption("계약서로는 알 수 없는 것들이에요. 직접 확인하기 전까지는 미확인으로 남아요. "
                   "확인했으면 체크해 두세요(이 화면에서만 기억해요).")
        for ck in res["checklist"]:
            checked = st.session_state.get(f"ck_{lid}_{ck['key']}", False)
            status = cc.SELF_CHECKED if checked else cc.UNCHECKED
            head = f"{'✓ ' if checked else ''}{ck['title']} · {status}" + (" · 꼭 확인" if ck["important"] and not checked else "")
            with st.expander(head, expanded=ck["important"] and not checked):
                st.markdown(ck["why"])
                if ck["hint"]:
                    st.markdown(f'<div class="sh-muted">계약서에서 본 것: {html.escape(ck["hint"])}</div>',
                                unsafe_allow_html=True)
                st.markdown("\n".join(f"- {h}" for h in ck["how"]))
                st.markdown(f'<div class="sh-muted">어디서: {html.escape(ck["where"])}</div>', unsafe_allow_html=True)
                if ck["key"] == "prior":
                    c = res["contract"]
                    st.markdown("**보증금 비율 계산 (참고)**")
                    k1, k2, k3 = st.columns(3)
                    with k1:
                        mort = st.number_input("등기부 근저당 채권최고액 (만 원)", min_value=0, step=100,
                                               value=None, key=f"mort_{lid}")
                    with k2:
                        prior = st.number_input("선순위 보증금 (만 원)", min_value=0, step=100,
                                                value=(c["prior"] // 10_000) if c["prior"] is not None else None,
                                                key=f"prior_{lid}")
                    with k3:
                        value = st.number_input("건물 시세 (만 원)", min_value=0, step=1000,
                                                value=(c["value"] // 10_000) if c["value"] else None,
                                                key=f"value_{lid}")
                    r = cc.deposit_ratio(None if mort is None else mort * 10_000,
                                         None if prior is None else prior * 10_000,
                                         c["deposit"], None if value is None else value * 10_000)
                    if r:
                        pct = f"{r['ratio']:.0%}"
                        st.markdown(f'{badge(r["level"], pct)} (근저당 + 선순위 + 내 보증금) ÷ 시세. '
                                    f'팀 기준 70% 이상 주의, 80% 이상 위험 (공식 기준 아님)', unsafe_allow_html=True)
                    else:
                        st.caption("등기부에서 근저당 금액을 확인해 넣으면 계산돼요. 숫자를 모르면 칸을 비워 두세요.")
                st.markdown(links(ck["sources"]), unsafe_allow_html=True)
                st.markdown(links(ck["cases"], "실제 사례 (참고용, 사기 판단 근거 아님)"), unsafe_allow_html=True)
                st.checkbox("직접 확인했어요", key=f"ck_{lid}_{ck['key']}")

    st.markdown("#### 집주인·중개사에게 보낼 문자")
    st.text_area("문자 초안", cc.message_draft(row["name"], res["questions"]), height=220,
                 key=f"msg_{lid}_{hash(contract_text)}_{bool(ai_state)}", label_visibility="collapsed")

    with st.expander("계약서 내용 직접 고치기"):
        st.caption("내용을 고치면 위의 표시가 바로 다시 계산돼요. 실제 계약서·개인정보는 넣지 마세요.")
        st.text_area("계약서", contracts[lid], height=360, key=f"ct_{lid}")
