# -*- coding: utf-8 -*-
"""
구내식당 입찰 알림 (광주·전남)
 나라장터 · 한전 전자조달 · 온비드 · 국방전자조달 · 코레일 · 전남대 · 대학/병원 게시판을 확인해
 새 공고가 있으면 윈도우 알림(+선택 텔레그램)을 보내고 입찰현황.html 에 모아 보여줌.
 PC에서도, GitHub Actions(리눅스)에서도 실행됨.
"""
import json, os, re, sys, html, hashlib, datetime as dt, traceback
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = Path(__file__).resolve().parent
CONFIG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
DATA_DIR = Path(os.environ.get("BID_DATA_DIR") or BASE)   # GitHub 에선 data/ 폴더
DATA_DIR.mkdir(parents=True, exist_ok=True)
SEEN_FILE = DATA_DIR / "seen.json"
DATA_FILE = DATA_DIR / "found.json"
HTML_FILE = DATA_DIR / "입찰현황.html"
LOG_FILE = DATA_DIR / "log.txt"
IS_WINDOWS = os.name == "nt"


def launch_browser(p):
    # PC: 설치된 Edge 사용 / 서버: Playwright 크로미움
    return p.chromium.launch(channel="msedge", headless=True) if IS_WINDOWS else p.chromium.launch(headless=True)

KEYWORDS = CONFIG["검색_키워드"]
REGIONS = CONFIG.get("지역_키워드", [])
EXCLUDE = CONFIG.get("제외_키워드", ["리모델링", "보수", "철거", "설계", "증축", "개선공사", "방수", "환기", "기계설비", "감리", "기술지도", "식자재", "폐기물",
                                        "김치류", "공산품", "축산물", "농산물", "수산물", "청소",
                                        "식재료", "급식재료", "부식", "쌀", "소모품", "냉난방기", "탑차", "기자재", "설치",
                                        "야채류", "어패류", "육계류", "육류", "주식류", "김치", "우유", "식기", "급식기구",
                                        "공조설비", "컨베이어", "급식물품", "급식용 식품", "물품구매", "카페트", "매점용품", "가구", "환경개선"])
DAYS = int(CONFIG.get("조회_기간_일", 14))
HISTORY_DAYS = int(CONFIG.get("지난공고_보관_일", 150))   # 약 5개월
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
STATE_FILE = DATA_DIR / "state.json"
STATE = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}


def _parse_dt(s):
    m = re.search(r"(\d{4})[-./](\d{2})[-./](\d{2})(?:\s+(\d{2}):(\d{2}))?", s or "")
    if not m:
        return None
    y, mo, d, h, mi = m.groups()
    return dt.datetime(int(y), int(mo), int(d), int(h or 23), int(mi or 59))


def is_past(x):
    """마감이 지났거나, 마감일을 모르면서 공고한 지 30일이 넘은 공고"""
    now = dt.datetime.now()
    close = _parse_dt(x.get("마감일"))
    if close:
        return close < now
    posted = _parse_dt(x.get("공고일"))
    return bool(posted and posted < now - dt.timedelta(days=30))


def log(msg):
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M}] {msg}"
    print(line)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


FOOD = CONFIG.get("음식_키워드", ["식당", "급식", "식음", "카페", "조리", "식사", "매점", "학생마루"])


def title_ok(title):
    # 검색_키워드로 찾되, 공고명에 음식 관련 단어가 있어야 함 ('위탁'만 있는 콜센터 위탁 등 제외)
    return any(k in title for k in FOOD) and not any(x in title for x in EXCLUDE)


# 권역 분류: '전남광주통합특별시'라는 이름에 '광주'가 들어가므로 단순 포함 검사로는 구분이 안 됨
GJ_ORGS = CONFIG.get("광주권_기관", ["전남대", "한전", "한국전력", "KDN", "한국에너지공과대"])
GJ_WORDS = CONFIG.get("광주권_지역", ["광주", "나주", "담양", "화순", "장성"])
GJ_DISTRICTS = ["동구", "서구", "남구", "북구", "광산구"]
JN_WORDS = CONFIG.get("전남권_지역", ["목포", "여수", "순천", "광양", "무안", "영암", "해남", "진도", "완도", "강진",
                                    "장흥", "보성", "고흥", "구례", "곡성", "함평", "영광", "신안"])
NOT_REGION = CONFIG.get("제외_지역", ["정읍", "전주", "군산", "익산", "남원", "김제", "경기도"])
MERGED = ("전남광주통합특별시", "광주전남통합특별시")


def region_of(text):
    """'광주권' / '전남권' / None"""
    if any(w in text for w in NOT_REGION):   # 서영대 파주캠퍼스처럼 다른 지역 캠퍼스는 먼저 제외
        return None
    if any(o in text for o in GJ_ORGS):
        return "광주권"
    merged = any(m in text for m in MERGED)
    t = text
    for m in MERGED:
        t = t.replace(m, " ")
    if any(w in t for w in JN_WORDS):
        return "전남권"
    if any(w in t for w in GJ_WORDS) or (merged and any(d in t for d in GJ_DISTRICTS)):
        return "광주권"
    if merged or "전남" in t or "전라남도" in t:
        return "전남권"
    return None


def region_ok(text):
    return region_of(text) is not None


# ───────────────────────── 1. 나라장터 ─────────────────────────
# 로그인 없이 나라장터 '입찰공고목록' 화면에서 공고명 검색 (최근 1개월, 화면 기본값)
G2B_MENU = "mf_wfm_gnb_wfm_gnbMenu_genDepth1_1_genDepth2_0_genDepth3_0_btn_menuLvl3"
G2B_P = "mf_wfm_container_tacBidPbancLst_contents_tab2_body_"


def fetch_g2b():
    from playwright.sync_api import sync_playwright
    out = {}
    with sync_playwright() as p:
        browser = launch_browser(p)
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.goto("https://www.g2b.go.kr", wait_until="load", timeout=60000)
        page.wait_for_selector("#" + G2B_MENU, state="attached", timeout=60000)
        page.wait_for_timeout(3000)
        page.evaluate(f"document.getElementById('{G2B_MENU}').click()")
        page.wait_for_selector(f"#{G2B_P}bidPbancNm", state="visible", timeout=60000)
        page.wait_for_timeout(2000)
        page.select_option(f"#{G2B_P}sbxRecordCountPerPage1", "100")
        page.wait_for_timeout(500)
        # 조회 기간을 1주 단위로 쪼갬 (한 번에 100건 제한). 처음 한 번은 5개월치, 이후엔 3주치
        span = HISTORY_DAYS if not STATE.get("g2b_history") else 21
        today = dt.date.today()
        windows = []
        end = today
        while (today - end).days < span:
            start = end - dt.timedelta(days=6)
            windows.append((start, end))
            end = start - dt.timedelta(days=1)
        results = []
        for (w_from, w_to) in windows:
            for kw in KEYWORDS:
                with page.expect_response(lambda r: "selectBidPbacScrollTypeList.do" in r.url, timeout=60000) as resp:
                    page.evaluate("""([p, kw, f, t]) => {
                        const byId = s => [...document.querySelectorAll('input')].find(i => i.offsetWidth && i.id.endsWith(s));
                        WebSquare.util.getComponentById(byId('ibxStrDay').id).setValue(f);
                        WebSquare.util.getComponentById(byId('ibxEndDay').id).setValue(t);
                        WebSquare.util.getComponentById(p + 'bidPbancNm').setValue(kw);
                        document.getElementById(p + 'btnS0004').click(); }""",
                                  [G2B_P, kw, w_from.strftime("%Y%m%d"), w_to.strftime("%Y%m%d")])
                try:
                    results += resp.value.json().get("result", []) or []
                except Exception:
                    pass
                page.wait_for_timeout(600)
        STATE["g2b_history"] = today.isoformat()
        if True:
            for it in results:
                title = html.unescape(it.get("bidPbancNm") or "")
                inst = html.unescape(it.get("dmstNm") or "")
                order = html.unescape(it.get("oderInstUntyGrpNm") or "")
                region = region_of(f"{title} {inst} {order}")
                if not title_ok(title) or not region:
                    continue
                if it.get("prcmBsneSeCdNm") == "공사":
                    continue  # 시설공사 제외
                dates = re.findall(r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}", it.get("pbancPstgDt") or "")
                dates = [d.replace("/", "-") for d in dates]
                no = it.get("bidPbancUntyNoOrd") or f'{it.get("bidPbancUntyNo")}-{it.get("bidPbancUntyOrd")}'
                out[no] = {
                    "id": "G2B:" + no, "출처": f"나라장터({it.get('prcmBsneSeCdNm', '')})", "공고명": title, "권역": region,
                    "기관": inst if inst == order or not order else f"{inst} ({order})",
                    "공고일": dates[0] if dates else "", "마감일": dates[1] if len(dates) > 1 else "",
                    "금액": it.get("prspPrce") or it.get("alotBgtAmt") or "",
                    "링크": "https://www.g2b.go.kr", "공고번호": no,
                }

        # 상세화면 핵심항목 요약 (이미 요약된 공고는 이전 것 재사용, 지난 공고는 생략)
        todo = []
        for no, x in out.items():
            prev = KNOWN.get(x["id"], {})
            if prev.get("요약"):
                x["요약"] = prev["요약"]
                x["마감일"] = prev.get("마감일", x["마감일"])
            elif not is_past(x):
                todo.append(x)
        for x in todo[:MAX_DETAIL]:
            try:
                g2b_detail(page, x)
            except Exception as e:
                log(f"나라장터 상세 실패 {x['공고번호']}: {e}")
        browser.close()
    return list(out.values())


MAX_DETAIL = 30
KNOWN = {}  # main() 에서 이전 결과로 채움

DETAIL_JS = r"""
() => {
  const val = td => { let s = td.innerText.replace(/\s+/g, ' ').trim();
    td.querySelectorAll('input[type=text],textarea').forEach(i => { if (i.value && !s.includes(i.value)) s += ' ' + i.value });
    return s.trim(); };
  const f = {};
  document.querySelectorAll('th').forEach(th => {
    if (!th.offsetWidth) return; const td = th.nextElementSibling;
    if (td && td.tagName === 'TD') f[th.innerText.replace(/\s+/g, ' ').trim()] = val(td); });
  const sched = [];
  [...document.querySelectorAll('table')].filter(t => t.offsetWidth && /입찰참가자격등록|개찰/.test(t.innerText))
    .forEach(t => t.querySelectorAll('tr').forEach(tr => {
      const c = [...tr.querySelectorAll('td')].map(td => td.innerText.trim());
      if (c.length >= 5 && /^\d+$/.test(c[0])) sched.push(c); }));
  const files = [...new Set([...document.querySelectorAll('td, a, span')].filter(e => e.offsetWidth && e.children.length === 0
      && /\.(hwp|hwpx|pdf|docx?|xlsx?|zip)$/i.test(e.innerText.trim())).map(e => e.innerText.trim()))];
  return {f, sched, files};
}
"""


def g2b_detail(page, x):
    num = x["공고번호"].split("-")[0]
    # 화면이 이전 공고 값을 남겨두므로 공고마다 페이지를 새로 연다
    page.goto("https://www.g2b.go.kr", wait_until="load", timeout=60000)
    page.wait_for_selector("#" + G2B_MENU, state="attached", timeout=60000)
    page.wait_for_timeout(2500)
    page.evaluate(f"document.getElementById('{G2B_MENU}').click()")
    page.wait_for_selector(f"#{G2B_P}ibxBidPbancNo", state="visible", timeout=60000)
    page.wait_for_timeout(1500)
    with page.expect_response(lambda r: "selectBidPbacScrollTypeList.do" in r.url, timeout=60000):
        page.evaluate("""([p, no]) => {
            WebSquare.util.getComponentById(p + 'bidPbancNm').setValue('');
            WebSquare.util.getComponentById(p + 'ibxBidPbancNo').setValue(no);
            document.getElementById(p + 'btnS0004').click(); }""", [G2B_P, num])
    page.wait_for_timeout(1500)
    clicked = page.evaluate("""(title) => {
        const a = [...document.querySelectorAll('table a')].filter(a => a.offsetWidth && a.textContent.trim().length > 4);
        const t = a.find(e => e.textContent.trim() === title) || a[0];
        if (t) { t.click(); return true } return false; }""", x["공고명"])
    if not clicked:
        raise RuntimeError("목록에서 공고를 찾지 못함")
    page.wait_for_function("""(no) => [...document.querySelectorAll('th')].some(th => th.offsetWidth
        && th.innerText.includes('입찰공고번호') && th.nextElementSibling && (th.nextElementSibling.innerText
        + [...th.nextElementSibling.querySelectorAll('input')].map(i => i.value).join('')).includes(no))""",
                           arg=num, timeout=60000)
    page.wait_for_timeout(2500)
    d = page.evaluate(DETAIL_JS)
    f = d["f"]

    def g(*keys):
        for k in keys:
            for fk, v in f.items():
                if fk.startswith(k) and v and v not in ("%", "0 원"):
                    return v
        return ""

    def bracket(s):
        m = re.findall(r"\[([^\]]+)\]", s)
        return ", ".join(m) if m else s[:80]

    s = {}
    s["계약방법"] = " / ".join(v for v in (g("계약방법"), g("낙찰방법")) if v)
    s["참가가능지역"] = bracket(g("참가가능지역")) or g("지역제한")
    s["업종제한"] = bracket(g("업종제한사항"))
    s["사업금액"] = g("사업금액")
    s["배정예산"] = g("배정예산")
    s["추정가격"] = g("추정가격")
    tech, price = g("기술능력평가비율").replace("%", "").strip(), g("입찰가격평가비율").replace("%", "").strip()
    if tech or price:
        s["평가비율"] = f"기술 {tech or '-'}% : 가격 {price or '-'}%"
    s["현장설명회"] = " ".join(v for v in (g("현장설명회일시"), g("현장설명회장소")) if v)
    s["공동수급"] = g("공동수급협정서")
    s["실적제한"] = g("실적제한여부")
    for c in d["sched"]:
        name, start, end = c[1], c[3] if len(c) > 3 else "", c[4] if len(c) > 4 else ""
        if name in ("입찰참가자격등록", "제안서제출", "입찰서제출", "개찰") and (start or end):
            s[name] = f"{start} ~ {end}".strip(" ~") if start and end else (end or start)
    if d["files"]:
        s["첨부파일"] = " · ".join(d["files"][:6])
    for k in ("사업금액", "배정예산", "추정가격"):  # '1 원' 같은 의미없는 금액 숨김
        n = re.sub(r"[^\d]", "", s.get(k, ""))
        if n and int(n) < 100000:
            s[k] = ""
    if s.get("배정예산") and s.get("배정예산") == s.get("사업금액"):
        s["배정예산"] = ""
    x["요약"] = {k: v for k, v in s.items() if v}
    deadline = s.get("입찰서제출") or s.get("제안서제출") or s.get("개찰") or ""
    m = re.findall(r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}", deadline)
    if m:
        x["마감일"] = m[-1].replace("/", "-")


# ───────────────────────── 2. 한전 전자조달 ─────────────────────────
KEPCO_JS = """
async ([kw, fromDate, toDate]) => {
  const c = Ext.ComponentQuery.query('*').find(x => x.getController && x.getController()
            && x.getController().search && x.getViewModel && x.getViewModel().get('searchValue'));
  const vm = c.getViewModel();
  vm.set('searchValue.searchType', 'AnnounceName');
  vm.set('searchValue.searchText', kw);
  vm.set('searchValue.fromSearchDate', new Date(fromDate));
  vm.set('searchValue.toSearchDate', new Date(toDate));
  vm.notify();
  c.getController().search();
}
"""


# 한국전력 + 나주 혁신도시에 본사가 있는 공동이용사 (한전KDN은 나라장터에서 검색)
KEPCO_COMPANIES = ("한국전력공사", "한전KPS", "한국전력거래소", "한전FMS", "한전CSC", "한국에너지공과대학교")


def kepco_region(path, title):
    """한전 계열은 전국에 사업소가 있으므로 회사명이 아니라 위치로 판단.
    위치 단어가 없으면, 본사(나주) 부서이거나 나주 본사 회사의 지역 사업소가 아닌 부서만 광주권"""
    text = f"{path} {title}"
    for o in GJ_ORGS:                 # '한전' 같은 기관명은 빼고 지역 단어로만 판단
        text = text.replace(o, " ")
    loc = region_of(text)
    if loc:
        return loc
    if "본사" in path:
        return "광주권"
    regional = (r"(서울|남서울|인천|경기|경기북부|강원|충북|대전|세종|충남|전북|대구|경북|부산|울산|경남|제주|발전)본부"
                r"|사업소|지사|발전소|원자력|변환소|연수원|청운재|인재개발원")
    if path.startswith(KEPCO_COMPANIES) and not re.search(regional, path + " " + title):
        return "광주권"
    return None


def fetch_kepco():
    from playwright.sync_api import sync_playwright
    end = dt.date.today()
    begin = end - dt.timedelta(days=HISTORY_DAYS)
    out = {}
    with sync_playwright() as p:
        browser = launch_browser(p)
        page = browser.new_page()
        page.goto("https://srm.kepco.net/index.do", wait_until="load", timeout=60000)
        page.frame_locator("iframe[name^='mdiiframe']").first.locator("a#JPA30500").click(timeout=60000)
        page.wait_for_function("""() => window.Ext && Ext.ComponentQuery.query('*').some(x => x.getController
            && x.getController() && x.getController().search && x.getViewModel && x.getViewModel().get('searchValue'))""",
                               timeout=60000)
        page.wait_for_timeout(1500)
        for kw in KEYWORDS:
            with page.expect_response(lambda r: "/router" in r.url and "getPageList" in (r.request.post_data or ""),
                                      timeout=60000) as resp:
                page.evaluate(KEPCO_JS, [kw, begin.isoformat(), end.isoformat()])
            data = resp.value.json()
            data = data if isinstance(data, list) else [data]
            for d in data:
                for rec in (d.get("result") or {}).get("records", []):
                    title = rec.get("announceName", "")
                    path = rec.get("departmentFullPath", "")
                    if not title_ok(title):
                        continue
                    region = kepco_region(path, title)
                    if not region:
                        continue
                    if rec.get("progressStatus") in ("Cancel",):
                        continue
                    no = rec.get("announceNo")
                    out[no] = {
                        "id": "KEPCO:" + no, "출처": "한전 전자조달", "공고명": title, "권역": region,
                        "기관": path, "공고일": (rec.get("announceDate") or "")[:10],
                        "마감일": (rec.get("endProposalDate") or "")[:16].replace("T", " "),
                        "금액": rec.get("presumedprice") or "",
                        "링크": "https://srm.kepco.net/index.do", "공고번호": no,
                        "본사": "본사" in path,
                    }
        browser.close()
    return list(out.values())


# ───────────────────────── 4. 온비드 (국유·공유재산 식당 사용허가/위탁) ─────────────────────────
ONBID_URL = "https://www.onbid.co.kr/op/cltrpbancinf/toppagemng/unfsrch/UnfSrchController/srchUnf.do"


def fetch_onbid():
    s = requests.Session()
    s.headers.update(UA)

    def search(div, kw, page=1):
        d = {"swd": kw, "srchSwd": kw, "pageIndex": str(page), "pageSize": "100", "srchDiv": div,
             "unfSrchExcnYn": "N", "srchBidPerdType": "0001", "srchPbancType": "0001", "srchOpbdType": "0001"}
        r = s.post(ONBID_URL, data=d, timeout=40)
        r.raise_for_status()
        return r.json()["unfSrchOtptVO"]

    clean = lambda t: html.unescape(re.sub(r"<[^>]+>", "", t or ""))
    cutoff = (dt.date.today() - dt.timedelta(days=HISTORY_DAYS)).isoformat()
    out = {}
    jobs = []   # (공고목록, 지역판단용 물건주소)
    for kw in KEYWORDS:
        # 진행 중 공고 + 입찰물건(주소 있음)으로 지역 판단
        local_pbanc = {}
        for c in search("bidCltr", kw).get("bidCltrSrchRslt", []) or []:
            addr = " ".join(str(c.get(k) or "") for k in ("sdnm", "sggnm", "zadrNm", "cltrRadr", "onbidCltrNm"))
            rg = region_of(addr)
            if rg:
                local_pbanc[c.get("onbidPbancNo")] = rg
        jobs.append((search("pbanc", kw).get("pbancSrchRslt", []) or [], local_pbanc))
    # 지난 공고(공고결과): 날짜 조건이 안 먹어서 '지역 + 키워드'로 검색 후 날짜로 거름
    for r_ in ("광주", "전남"):
        for k in ("식당", "구내식당", "급식", "카페", "식음"):
            for pg in (1, 2, 3):
                j = search("pbancRslt", f"{r_} {k}", pg)
                rows = j.get("pbancRsltSrchRslt", []) or []
                jobs.append((rows, {}))
                if len(rows) < 100:
                    break
    for rows, local_pbanc in jobs:
        for p in rows:
            title, org = clean(p.get("onbidPbancNm")), clean(p.get("regOrgNm"))
            if p.get("dspsMthodNm") != "임대" or not title_ok(title):
                continue
            if (p.get("pbancDt") or "")[:10] < cutoff:
                continue
            region = local_pbanc.get(p.get("onbidPbancNo")) or region_of(f"{title} {org}")
            if not region:
                continue
            no = p.get("pbancMngNo") or str(p.get("onbidPbancNo"))
            summ = {
                "공고종류": " ".join(v for v in (p.get("pbancKindNm"), p.get("cltrPrptDivNm"), p.get("dspsMthodNm")) if v),
                "입찰기간": f"{p.get('pbctBgngDt', '')} ~ {p.get('pbctDdlnDt', '')}".strip(" ~"),
                "개찰": p.get("pbctExctDt") or "",
                "담당": " ".join(v for v in (org, p.get("sbrNm")) if v),
                "진행상태": p.get("exctStatNm") or "",
            }
            out[no] = {
                "id": "ONBID:" + no, "출처": "온비드", "공고명": title, "기관": org, "권역": region,
                "공고일": (p.get("pbancDt") or "")[:10], "마감일": p.get("pbctDdlnDt") or "",
                "금액": "", "공고번호": no, "링크": "https://www.onbid.co.kr",
                "요약": {k: v for k, v in summ.items() if v},
            }
    return list(out.values())


# ───────────────────────── 5. 국방전자조달 (용역 입찰: 병영식당 민간위탁 급식 등) ─────────────────────────
D2B_URL = "https://www.d2b.go.kr/psb/bid/getServiceBidAnnounceListNew.json"


def fetch_d2b():
    s = requests.Session()
    s.headers.update({**UA, "X-Requested-With": "XMLHttpRequest"})
    t = dt.date.today()
    out = {}
    fmt = lambda d: f"{d[:4]}-{d[4:6]}-{d[6:8]}" if d and len(d) >= 8 and d[:8].isdigit() else ""
    for kw in dict.fromkeys(KEYWORDS + ["병영식당", "취사"]):
        d = {"date_divs": "1", "date_from": (t - dt.timedelta(days=HISTORY_DAYS)).strftime("%Y%m%d"),
             "date_to": (t + dt.timedelta(days=90)).strftime("%Y%m%d"), "anmt_name": kw,
             "area_name": "광주", "numb_divs": "1", "currentPageNo": "1"}  # area '광주' = 광주·전남 지역 부대
        r = s.post(D2B_URL, data=d, timeout=40)
        r.raise_for_status()
        for x in r.json().get("list", []) or []:
            title, org = x.get("rpstItnm") or "", x.get("codeVld3") or ""
            if not title_ok(title) or x.get("anmtCncl") == "Y":
                continue
            no = x.get("bidxAnbr") or f"{x.get('anmtNumb')}-{x.get('rqstDegr')}"
            region = region_of(f"{title} {org}") or ("전남권" if "함대" in org else "광주권")
            close = fmt(x.get("bidxCldt") or "")
            if close and x.get("bidxCltm", "").isdigit():
                close += f" {x['bidxCltm'][:2]}:{x['bidxCltm'][2:4]}"
            out[no] = {
                "id": "D2B:" + no, "출처": "국방전자조달", "공고명": title, "기관": org, "권역": region,
                "공고일": fmt(x.get("anmtDate") or ""), "마감일": close, "금액": x.get("bsicExpt") or "",
                "공고번호": no, "링크": "https://www.d2b.go.kr/psb/bid/serviceBidAnnounceList.do?key=137",
                "요약": {k: v for k, v in {"공고구분": x.get("codeVld1"), "계약방법": x.get("codeVld4"),
                                          "입찰방식": x.get("codeVld2"), "발주부대": org}.items() if v},
            }
    return list(out.values())


# ───────────────────────── 7. 코레일 전자조달 (ebid.korail.com) ─────────────────────────
KORAIL_URL = "https://ebid.korail.com/comnRfc.do"


def fetch_korail():
    """공고명 검색이 서버에서 안 먹어서, 용역(5)·물품(1) 공고를 90일 단위로 모두 받아 걸러냄"""
    s = requests.Session()
    s.headers.update(UA)
    s.get("https://ebid.korail.com/goods/inList.do", timeout=30)
    today = dt.date.today()
    windows, end = [], today
    while (today - end).days < HISTORY_DAYS:
        start = end - dt.timedelta(days=89)
        windows.append((start, end))
        end = start - dt.timedelta(days=1)
    fmt = lambda d: f"{d[:4]}-{d[4:6]}-{d[6:8]}" if d and len(d) == 8 and d != "00000000" else ""
    out = {}
    for typ in ("5", "1"):
        for w_from, w_to in windows:
            vals = {"I_ZZBIDTYPECODE": typ, "I_ERDAT_FR": w_from.strftime("%Y%m%d"), "I_ERDAT_TO": w_to.strftime("%Y%m%d")}
            names = ["I_ZZBIDTYPECODE", "I_ZZCTRMETHOD", "I_BSART", "I_ZZBIDINV", "I_DESCRIPTION",
                     "I_ERDAT_FR", "I_ERDAT_TO", "I_SDAT_FR", "I_SDAT_TO", "I_LIFNR"]
            p = [{"value_string": vals.get(n, ""), "name": n} for n in names]
            r = s.post(KORAIL_URL, data={"p": json.dumps(p, ensure_ascii=False),
                                         "fn": json.dumps({"ZFUNCNM": "ZMME_EBID_INFO_0009F"})}, timeout=60)
            r.raise_for_status()
            itab = {x["name"]: x["value"] for x in json.loads(r.json()["data"])["itab"]}
            for x in itab.get("ET_ZSMMEEBID0083") or []:
                title = x.get("description") or ""
                if not title_ok(title):
                    continue
                region = region_of(title) or region_of(x.get("eknam") or "")
                if not region:
                    continue
                no = f"{x.get('zzbidinv')}"
                close = fmt(x.get("zzbid_edat"))
                if close and x.get("zzbid_etim"):
                    close += " " + x["zzbid_etim"][:5]
                out[no] = {
                    "id": "KORAIL:" + no, "출처": "코레일", "공고명": title, "기관": f"코레일 {x.get('eknam', '')}",
                    "권역": region, "공고일": fmt(x.get("zzbid_erdat")), "마감일": close, "금액": "",
                    "공고번호": no, "링크": "https://ebid.korail.com/goods/inList.do",
                    "요약": {k: v for k, v in {"계약방법": x.get("zzctrmethod_t"), "낙찰방법": x.get("zzwayofbidding_t"),
                                              "담당": x.get("eknam")}.items() if v},
                }
    return list(out.values())


# ───────────────────────── 6. 대학·병원 홈페이지 입찰 게시판 ─────────────────────────
BOARDS = CONFIG.get("게시판", {
    "전남대병원": "https://www.cnuh.com/cnuh/board/B0012.cs",
    "조선대": "https://www.chosun.ac.kr/user/boardList.do?boardId=10513",
    "호남대": "https://www.honam.ac.kr/GeneralNotice/category44",
    "광주여대": "https://www.kwu.ac.kr/mod/board/list.do?MID=KWU_B06010101",
    "동신대": "https://www.dsu.ac.kr/kr/index.php?pCode=bid",
    "광주보건대": "https://www.ghu.ac.kr/BiddingAnnouncement",
    "광주대": "https://www.gwangju.ac.kr/bbs/?b_id=gwangju_notice_no&site=gwangju&mn=536",
})

BOARD_JS = r"""
() => {
  const rows = [];
  document.querySelectorAll('tr, li').forEach(el => {
    if (el.querySelector('tr, li')) return;                       // 가장 안쪽 행만
    const txt = el.innerText.replace(/\s+/g, ' ').trim();
    const m = txt.match(/20\d\d[-.\/]\d\d[-.\/]\d\d/);
    const links = [...el.querySelectorAll('a[href]')].filter(a => a.innerText.trim().length > 4);
    if (!m || !links.length) return;
    const a = links.sort((x, y) => y.innerText.length - x.innerText.length)[0];
    rows.push({title: (a.getAttribute('title') || a.innerText).replace(/\s+/g, ' ').trim(), href: a.href, date: m[0]});
  });
  return rows;
}
"""


def fetch_boards():
    from playwright.sync_api import sync_playwright
    out, errors = {}, []
    with sync_playwright() as p:
        browser = launch_browser(p)
        page = browser.new_page()
        for name, url in BOARDS.items():
            try:
                page.goto(url, wait_until="load", timeout=45000)
                page.wait_for_timeout(2500)
                for r in page.evaluate(BOARD_JS):
                    title = r["title"]
                    if not title_ok(title):
                        continue
                    key = hashlib.md5(re.sub(r"[&?](page|pageIndex)=\d+", "", r["href"]).encode()).hexdigest()[:12]
                    date = r["date"].replace(".", "-").replace("/", "-")
                    out[name + key] = {
                        "id": f"BOARD:{name}:{key}", "출처": name, "공고명": title, "기관": name, "권역": "광주권",
                        "공고일": date, "마감일": "", "금액": "", "공고번호": "", "링크": r["href"],
                    }
            except Exception as e:
                errors.append(f"{name}({str(e)[:40]})")
        browser.close()
    if errors:
        log("게시판 일부 실패: " + ", ".join(errors))
    return list(out.values())


# ───────────────────────── 3. 전남대학교 ─────────────────────────
JNU_LIST = "https://www.jnu.ac.kr/WebApp/web/HOM/COM/Board/board.aspx?boardID=5&cate=12&page={}"


def fetch_jnu():
    out = []
    for pg in (1, 2):
        r = requests.get(JNU_LIST.format(pg), headers=UA, timeout=40)
        soup = BeautifulSoup(r.text, "lxml")
        for tr in soup.select("table tbody tr"):
            a = tr.find("a", href=True)
            if not a:
                continue
            title = a.get_text(" ", strip=True)
            m = re.search(r"key=(\d+)", a["href"])
            if not m or not title_ok(title):
                continue
            date = re.search(r"\d{4}-\d{2}-\d{2}", tr.get_text(" "))
            out.append({
                "id": "JNU:" + m.group(1), "출처": "전남대 홈페이지", "공고명": title, "권역": "광주권",
                "기관": "전남대학교", "공고일": date.group(0) if date else "", "마감일": "",
                "금액": "", "공고번호": m.group(1),
                "링크": f"https://www.jnu.ac.kr/WebApp/web/HOM/COM/Board/board.aspx?boardID=5&bbsMode=view&key={m.group(1)}",
            })
    return out


# ───────────────────────── 알림 / 화면 ─────────────────────────
def notify_windows(new):
    if not IS_WINDOWS:
        return
    try:
        from winotify import Notification, audio
        lines = "\n".join(f"· [{x.get('권역', '')}] {x['공고명'][:36]}" for x in new[:4])
        more = f"\n외 {len(new) - 4}건" if len(new) > 4 else ""
        n = Notification(app_id="구내식당 입찰알림", title=f"새 식당 입찰 {len(new)}건",
                         msg=lines + more, launch=HTML_FILE.as_uri(), duration="long")
        n.set_audio(audio.Default, loop=False)
        n.add_actions(label="모아보기 열기", launch=HTML_FILE.as_uri())
        n.show()
    except Exception as e:
        log(f"윈도우 알림 실패: {e}")


def notify_telegram(new):
    token = (os.environ.get("TELEGRAM_TOKEN") or CONFIG.get("텔레그램_봇토큰", "")).strip()
    chat = (os.environ.get("TELEGRAM_CHAT_ID") or CONFIG.get("텔레그램_채팅ID", "")).strip()
    if not (token and chat):
        return
    for x in new:
        text = f"🍽 [{x['출처']}] {x['공고명']}\n기관: {x['기관']}\n마감: {x['마감일'] or '-'}\n{x['링크']}"
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": text}, timeout=20)
        except Exception as e:
            log(f"텔레그램 실패: {e}")


def money(v):
    try:
        return f"{int(float(v)):,}원" if v and float(v) > 1 else ""
    except Exception:
        return str(v)


def summary_html(x):
    s = x.get("요약")
    if not s:
        return ""
    head = " · ".join(v for v in (s.get("참가가능지역"), s.get("사업금액") or s.get("배정예산") or s.get("추정가격")) if v)
    rows = "".join(f"<tr><th>{html.escape(k)}</th><td>{html.escape(v)}</td></tr>" for k, v in s.items())
    return (f"<details class=sum><summary>공고 요약 {('— ' + html.escape(head)) if head else ''}</summary>"
            f"<table>{rows}</table></details>")


TABS = ("광주권", "전남권", "지난 공고")


def write_html(all_items, new_ids, status):
    panels, tabs = [], []
    for i, tab in enumerate(TABS):
        if tab == "지난 공고":
            items = [x for x in all_items if is_past(x)]
        else:
            items = [x for x in all_items if x.get("권역", "광주권") == tab and not is_past(x)]
        rows = []
        for x in sorted(items, key=lambda x: x.get("공고일", ""), reverse=True):
            badge = '<span class="new">NEW</span>' if x["id"] in new_ids else ""
            star = '<span class="hq">본사</span>' if x.get("본사") else ""
            src = html.escape(x['출처']) + (f"<div class=sub>{html.escape(x.get('권역', ''))}</div>" if tab == "지난 공고" else "")
            rows.append(
                f"<tr><td>{src}</td>"
                f"<td>{badge}{star}<a href=\"{html.escape(x['링크'])}\" target=\"_blank\">{html.escape(x['공고명'])}</a>"
                f"<div class=sub>공고번호 {html.escape(str(x['공고번호']))}</div>{summary_html(x)}</td>"
                f"<td>{html.escape(x['기관'])}</td><td>{html.escape(x['공고일'])}</td>"
                f"<td>{html.escape(x['마감일'])}</td><td class=r>{money(x['금액'])}</td></tr>")
        n_new = sum(1 for x in items if x["id"] in new_ids)
        new_tag = f' <span class="new">{n_new}</span>' if n_new else ""
        label = f"지난 공고 · {HISTORY_DAYS // 30}개월" if tab == "지난 공고" else f"{tab} 진행중"
        tabs.append(f'<button class="tab{" on" if i == 0 else ""}" data-t="{tab}">{label} ({len(items)}){new_tag}</button>')
        panels.append(
            f'<div class="panel" id="p-{tab}"{"" if i == 0 else " hidden"}>'
            f'<table class=main><tr><th>출처</th><th>공고명</th><th>기관</th><th>공고일</th><th>마감</th><th>금액</th></tr>'
            f'{"".join(rows) or "<tr><td colspan=6>조건에 맞는 공고가 없습니다.</td></tr>"}</table></div>')
    rows_html = '<div class="tabs">' + "".join(tabs) + "</div>" + "".join(panels)
    st = "".join(f"<li>{html.escape(k)}: {html.escape(v)}</li>" for k, v in status.items())
    HTML_FILE.write_text(f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<title>구내식당 입찰현황</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{{font-family:'Malgun Gothic',sans-serif;margin:24px;background:#f6f7f9;color:#222}}
h1{{font-size:22px;margin:0 0 4px}} .meta{{color:#666;font-size:13px;margin-bottom:14px}}
table{{border-collapse:collapse;width:100%;background:#fff;font-size:14px}}
th,td{{border-bottom:1px solid #e3e5e8;padding:9px 8px;text-align:left;vertical-align:top}}
th{{background:#2f4b7c;color:#fff;position:sticky;top:0}} tr:hover td{{background:#f0f4ff}}
a{{color:#1a4fb3;text-decoration:none;font-weight:600}} a:hover{{text-decoration:underline}}
.sub{{color:#888;font-size:12px}} .r{{text-align:right;white-space:nowrap}}
.new{{background:#e0313b;color:#fff;border-radius:4px;padding:1px 6px;font-size:11px;margin-right:6px}}
.hq{{background:#f0a500;color:#fff;border-radius:4px;padding:1px 6px;font-size:11px;margin-right:6px}}
ul{{font-size:12px;color:#555}}
details.sum{{margin-top:6px;font-size:13px}} details.sum summary{{cursor:pointer;color:#2f4b7c;font-weight:600}}
details.sum table{{margin-top:6px;background:#f8f9fb;width:100%}}
details.sum th{{background:#e8ecf3;color:#333;position:static;width:110px;font-weight:600;padding:5px 8px}}
details.sum td{{padding:5px 8px}}
.tabs{{display:flex;gap:4px;margin-bottom:-1px}}
.tab{{font:inherit;font-size:15px;font-weight:600;padding:9px 20px;border:1px solid #d5d9e0;border-bottom:none;
  background:#e9edf3;color:#555;border-radius:8px 8px 0 0;cursor:pointer}}
.tab.on{{background:#2f4b7c;color:#fff;border-color:#2f4b7c}}
.tab .new{{margin:0 0 0 4px}}
</style></head><body>
<h1>🍽 구내식당 입찰현황</h1>
<div class="meta">마지막 확인 {dt.datetime.now():%Y-%m-%d %H:%M} · 총 {len(all_items)}건 · 새 공고 {len(new_ids)}건</div>
{rows_html}
<h3 style="font-size:14px;margin:22px 0 4px;color:#444">사이트별 확인 상태</h3>
<ul>{st}</ul>
<script>
document.querySelectorAll('.tab').forEach(b => b.onclick = () => {{
  document.querySelectorAll('.tab').forEach(x => x.classList.toggle('on', x === b));
  document.querySelectorAll('.panel').forEach(p => p.hidden = p.id !== 'p-' + b.dataset.t);
  try {{ localStorage.setItem('tab', b.dataset.t) }} catch (e) {{}}
}});
try {{ const t = localStorage.getItem('tab'); if (t) document.querySelector('.tab[data-t="' + t + '"]')?.click(); }} catch (e) {{}}
</script></body></html>""", encoding="utf-8")


def main():
    seen = set(json.loads(SEEN_FILE.read_text(encoding="utf-8"))) if SEEN_FILE.exists() else set()
    found = {x["id"]: x for x in json.loads(DATA_FILE.read_text(encoding="utf-8"))} if DATA_FILE.exists() else {}
    KNOWN.update(found)
    html_only = "--html-only" in sys.argv      # 수집 없이 저장된 결과로 웹페이지만 다시 만듦
    where = "PC" if IS_WINDOWS else "서버"
    now_s = f"{dt.datetime.now():%m-%d %H:%M}"
    src_state = STATE.setdefault("출처상태", {})   # 출처별 마지막 성공 기록 (PC·서버 결과 합치기용)
    sources = (("나라장터", fetch_g2b), ("한전 전자조달", fetch_kepco), ("온비드", fetch_onbid), ("국방전자조달", fetch_d2b),
               ("코레일", fetch_korail), ("전남대", fetch_jnu), ("대학·병원 게시판", fetch_boards))
    for name, fn in ([] if html_only else sources):
        try:
            items = fn()
            for x in items:
                prev = found.get(x["id"], {})
                if prev.get("요약") and not x.get("요약"):
                    x["요약"] = prev["요약"]
                found[x["id"]] = x
            src_state[name] = {"ok": f"{now_s} {where} ({len(items)}건)", "err": ""}
        except Exception as e:
            src_state.setdefault(name, {})["err"] = f"{now_s} {where}: {str(e)[:80]}"
            log(f"{name} 오류: {e}\n{traceback.format_exc()}")
    status = {}
    for name, _ in sources:
        s = src_state.get(name, {})
        txt = f"마지막 정상 확인 {s['ok']}" if s.get("ok") else "아직 확인 못함"
        if s.get("err"):
            txt += f" / 최근 실패 {s['err']}"
        status[name] = txt
    # 오래된 공고 정리 (약 5개월)
    cutoff = (dt.date.today() - dt.timedelta(days=HISTORY_DAYS)).isoformat()
    found = {k: v for k, v in found.items() if (v.get("공고일") or "9999") >= cutoff and title_ok(v["공고명"])}
    for v in found.values():  # 예전 저장분 권역 채우기
        if "권역" not in v:
            v["권역"] = region_of(f"{v['공고명']} {v['기관']}")
    found = {k: v for k, v in found.items() if v.get("권역")}

    unseen = [] if html_only else [x for k, x in found.items() if k not in seen]
    new = [x for x in unseen if not is_past(x)]          # 알림은 진행 중 공고만
    for x in unseen:
        x["발견"] = dt.datetime.now().isoformat(timespec="minutes")
    recent = (dt.datetime.now() - dt.timedelta(hours=24)).isoformat(timespec="minutes")
    new_ids = {x["id"] for x in found.values() if x.get("발견", "") >= recent and not is_past(x)}
    write_html(list(found.values()), new_ids, status)
    if html_only:
        log("웹페이지만 다시 만듦")
        return
    DATA_FILE.write_text(json.dumps(list(found.values()), ensure_ascii=False, indent=1), encoding="utf-8")
    STATE_FILE.write_text(json.dumps(STATE, ensure_ascii=False), encoding="utf-8")
    if new:
        notify_windows(new)
        notify_telegram(new)
    if unseen:
        SEEN_FILE.write_text(json.dumps(sorted(seen | {x["id"] for x in unseen}), ensure_ascii=False), encoding="utf-8")
    log(f"완료({where}): 새 공고 {len(new)}건 (지난 공고 {len(unseen) - len(new)}건 조용히 추가) / "
        + ", ".join(f"{k}: {v}" for k, v in status.items()))
    if "--open" in sys.argv:
        os.startfile(HTML_FILE)


if __name__ == "__main__":
    main()
