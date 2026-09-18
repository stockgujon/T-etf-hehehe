#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抓取「國內掛牌ETF(上市+上櫃)」清單，以及每檔ETF的除息事件(日期、金額)，
推估配息頻率，輸出成 data/dividends.json 與 data/etf_meta.json 給前端讀取。

資料來源：
  1. ETF清單：TWSE ISIN 公開資訊查詢頁 (isin.twse.com.tw)
     strMode=2 -> 上市 (TWSE) 全部有價證券
     strMode=4 -> 上櫃 (TPEx) 全部有價證券
     從中篩選「產業別」為 ETF 的列。
  2. ETF分類(category)：TWSE「e添富」平台每檔ETF自己的商品頁
     (https://www.twse.com.tw/zh/ETFortune/etfInfo/<代號>)，
     裡面「證券類別」欄位是官方分類(如「股票槓反ETF」「台股ETF」)，
     「主題/因子」欄位會列出「高股息」「高息低波動」等標籤。
     用這兩個欄位分成：槓桿反向、債券型、商品期貨型、高股息、一般股票型。
     抓不到才退回用ETF名稱關鍵字猜測(LEVERAGE_INVERSE_HINTS等)。
  3. 已發生的除息事件(status=actual)：Yahoo Finance chart API 的 dividends 事件
     上市 ETF 用 {代號}.TW，上櫃 ETF 用 {代號}.TWO
  4. 已公告但尚未除息的事件(status=announced)：TWSE「e添富」平台的公告列表
     (https://www.twse.com.tw/zh/ETFortune/announcementList)，
     這是全市場(上市+上櫃ETF)共用的一份「全部類別」公告列表，只掃最近約35天，
     不篩證交所自己的分類(因為分類不可靠)，改成每篇都嘗試從內文解析
     「除息交易日」與「配發金額」，能解析出來的才算數。
     同一檔同一天如果Yahoo已經有實際數字，以實際數字為準，公告預估的那筆會被捨棄。

這些都不是官方「文件化」的公開API，是社群長期驗證可用/我方直接觀察頁面格式後
歸納出的公開端點；若哪天格式跑掉，屬於預期中會需要調整的維護點，程式已盡量寫得
寬容(找不到欄位就跳過該筆，不會讓整個流程死掉)。

重試策略：每一次HTTP請求本身有內建重試(數次、間隔遞增)；
若整個腳本仍然失敗(例如來源網站當下完全打不通)，會以非0狀態碼結束，
交給 GitHub Actions 那一層再等一段時間後重跑一次整個 job。
"""

import json
import re
import sys
import time
import unicodedata
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from html import unescape as html_unescape
from html.parser import HTMLParser

TAIPEI_TZ = timezone(timedelta(hours=8))

ISIN_URLS = {
    "TWSE": "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2",
    "TPEx": "https://isin.twse.com.tw/isin/C_public.jsp?strMode=4",
}

# 用來猜測ETF類別的關鍵字，只在「官方分類頁面抓不到」時當備援使用
LEVERAGE_INVERSE_HINTS = ["正2", "正二", "反1", "反一", "槓桿", "2X", "反向"]
BOND_HINTS = ["债", "債", "公司債", "美債", "公債", "投等債", "高收債"]
COMMODITY_HINTS = ["原油", "黃金", "黄金", "白銀", "商品", "期货", "期貨", "石油"]
HIGH_DIVIDEND_HINTS = ["高股息", "高息", "優息", "優利", "高填息"]

ETF_INFO_URL = "https://www.twse.com.tw/zh/ETFortune/etfInfo/{code}"
HIGH_DIVIDEND_TAGS = {"高股息", "高息低波動"}

USER_AGENT = "Mozilla/5.0 (compatible; tw-etf-dividend-tracker/1.0)"

ANNOUNCEMENT_LIST_URL = "https://www.twse.com.tw/zh/ETFortune/announcementList?max=10&offset={offset}"
ANNOUNCEMENT_LOOKBACK_DAYS = 35  # 抓「近一個月」的公告，多留一點緩衝
ANNOUNCEMENT_MAX_PAGES = 40  # 安全上限，避免萬一日期判斷失準時無限翻頁

ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
HREF_RE = re.compile(r'href="(/zh/ETFortune/announcement\?[^"]+)"')
TAG_RE = re.compile(r"<[^>]+>")
EX_DATE_RE = re.compile(r"除息交易日[:：]?\s*([0-9]{2,3})[/\-]([0-9]{1,2})[/\-]([0-9]{1,2})")
AMOUNT_RE = re.compile(r"新[臺台]幣\s*([0-9]+(?:\.[0-9]+)?)\s*元")
AMOUNT_KEYWORD_RE = re.compile(r"預計|預估|預定|實際")


def http_get(url, max_retries=4, base_delay=20, encoding=None):
    """帶重試的 HTTP GET，回傳文字內容。"""
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                if encoding:
                    return raw.decode(encoding, errors="ignore")
                return raw.decode("utf-8", errors="ignore")
        except Exception as e:  # noqa: BLE001 - 抓取來源很多種例外，統一重試
            last_err = e
            wait = base_delay * attempt
            print(f"[warn] GET失敗({attempt}/{max_retries}): {url} -> {e}；{wait}s後重試", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"多次重試仍失敗: {url} ({last_err})")


class ISINTableParser(HTMLParser):
    """解析 isin.twse.com.tw 的公開清單表格，一列一列收集儲存格文字。"""

    def __init__(self):
        super().__init__()
        self.rows = []
        self._cur_row = None
        self._cur_cell = None
        self._in_cell = False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._cur_row = []
        elif tag in ("td", "th"):
            self._in_cell = True
            self._cur_cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            if self._cur_row is not None and self._cur_cell is not None:
                text = "".join(self._cur_cell).strip()
                text = unicodedata.normalize("NFKC", text)
                self._cur_row.append(text)
            self._in_cell = False
            self._cur_cell = None
        elif tag == "tr":
            if self._cur_row:
                self.rows.append(self._cur_row)
            self._cur_row = None

    def handle_data(self, data):
        if self._in_cell and self._cur_cell is not None:
            self._cur_cell.append(data)


def guess_category(name: str) -> str:
    """名稱關鍵字猜測，只在官方分類頁面抓不到時當備援。"""
    for kw in LEVERAGE_INVERSE_HINTS:
        if kw in name:
            return "槓桿反向"
    for kw in BOND_HINTS:
        if kw in name:
            return "債券型"
    for kw in COMMODITY_HINTS:
        if kw in name:
            return "商品期貨型"
    for kw in HIGH_DIVIDEND_HINTS:
        if kw in name:
            return "高股息"
    return "一般股票型"


def fetch_etf_classification(code: str):
    """抓該ETF在e添富的商品頁，回傳 (證券類別原始字串, 主題/因子標籤list)。
    抓不到或格式跑掉就回傳 (None, [])，呼叫端會退回用名稱關鍵字猜測。
    """
    url = ETF_INFO_URL.format(code=code)
    try:
        html = http_get(url, max_retries=2, base_delay=8)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] {code} 分類頁抓取失敗，退回用名稱猜測: {e}", file=sys.stderr)
        return None, []

    text = strip_tags(html)
    text = re.sub(r"[ \t]+", " ", text)

    asset_type = None
    m = re.search(r"證券類別\s*([^\s]*?ETF)", text)
    if m:
        asset_type = m.group(1)

    tags = []
    m2 = re.search(r"主題/?因子[:：]?\s*(.*?)資產規模", text, re.S)
    if m2:
        tag_blob = re.sub(r"\s+", " ", m2.group(1)).strip()
        tags = [t for t in tag_blob.split(" ") if t]

    return asset_type, tags


def classify_etf(name: str, asset_type: str, tags: list) -> str:
    """依官方「證券類別」+「主題/因子」標籤決定分類，抓不到才退回名稱猜測。"""
    if asset_type:
        if any(kw in asset_type for kw in ("槓反", "槓桿", "反向")):
            return "槓桿反向"
        if "債" in asset_type:
            return "債券型"
        if any(kw in asset_type for kw in ("期貨", "商品")):
            return "商品期貨型"
        if any(t in HIGH_DIVIDEND_TAGS for t in tags):
            return "高股息"
        return "一般股票型"
    return guess_category(name)


def fetch_etf_list(market: str) -> list:
    """回傳 [{code, name, market, category}, ...]"""
    html = http_get(ISIN_URLS[market], encoding="ms950")
    parser = ISINTableParser()
    parser.feed(html)

    etfs = []
    for row in parser.rows:
        if len(row) < 5:
            continue
        code_name = row[0]
        industry = row[4] if len(row) > 4 else ""
        # 該欄格式通常類似 "0050　元大台灣50"（中間是全形空白或Tab）
        m = re.match(r"^([0-9A-Za-z]{4,6})[\s　]+(.+)$", code_name)
        if not m:
            continue
        code, name = m.group(1), m.group(2).strip()
        is_etf = ("ETF" in industry) or bool(re.match(r"^00\d{2,3}[A-Z]?$", code))
        if not is_etf:
            continue
        etfs.append({
            "code": code,
            "name": name,
            "market": market,
            "category": guess_category(name),
        })

    # 去重（同代號只留一筆）
    seen = {}
    for e in etfs:
        seen[e["code"]] = e
    result = list(seen.values())
    print(f"[info] {market} 抓到 {len(result)} 檔 ETF")
    return result


def fetch_dividend_events(code: str, market: str) -> list:
    """回傳該ETF近兩年的除息事件 [{ex_date: 'YYYY-MM-DD', amount: float}, ...]"""
    suffix = ".TW" if market == "TWSE" else ".TWO"
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{code}{suffix}"
        f"?range=2y&interval=1d&events=div"
    )
    try:
        text = http_get(url, max_retries=3, base_delay=15)
        data = json.loads(text)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] {code}{suffix} 除息資料抓取失敗，略過: {e}", file=sys.stderr)
        return []

    try:
        result = data["chart"]["result"][0]
        dividends = result.get("events", {}).get("dividends", {})
    except (KeyError, IndexError, TypeError):
        return []

    events = []
    for _, ev in dividends.items():
        try:
            ts = ev["date"]
            amount = float(ev["amount"])
            dt = datetime.fromtimestamp(ts, tz=TAIPEI_TZ).date()
            events.append({"ex_date": dt.isoformat(), "amount": round(amount, 4)})
        except (KeyError, ValueError, TypeError):
            continue

    events.sort(key=lambda x: x["ex_date"])
    return events


def infer_frequency(events: list) -> dict:
    """根據近12~18個月的除息事件筆數，粗估配息頻率與通常配息月份。"""
    if not events:
        return {"frequency": "未知", "freq_months": []}

    today = datetime.now(TAIPEI_TZ).date()
    cutoff = today - timedelta(days=548)  # 近18個月
    recent = [e for e in events if datetime.fromisoformat(e["ex_date"]).date() >= cutoff]
    if not recent:
        recent = events[-4:]

    months = sorted({datetime.fromisoformat(e["ex_date"]).date().month for e in recent})
    # 用最近12個月的事件數估頻率，再退回18個月的資料估算
    count = len(recent)
    span_years = max(len(recent) / 12.0, 0.5)
    per_year = count / span_years if span_years else count

    if per_year >= 10:
        freq = "月配"
    elif per_year >= 5:
        freq = "雙月配"
    elif per_year >= 3:
        freq = "季配"
    elif per_year >= 1.5:
        freq = "半年配"
    else:
        freq = "年配"

    return {"frequency": freq, "freq_months": months}


def strip_tags(html_fragment: str) -> str:
    return TAG_RE.sub(" ", html_fragment)


def parse_announcement_rows(html: str) -> list:
    """解析公告列表頁一頁的內容，回傳 [{fund, date(YYYYMMDD), seq, type, href}, ...]。

    這裡抓的是「全部類別」的公告列表，不篩分類。
    原因：證交所自己的分類欄位不可靠，有些內容明明是收益分配(有除息交易日+
    配發金額)，卻被歸到別的type(甚至type=all)，只篩type=distribution會漏掉。
    改成：先把候選公告都收集起來，實際過不過關由後面「能不能從內文解析出
    除息交易日+金額」來決定，這樣不管證交所怎麼分類都不影響。
    """
    matches = list(HREF_RE.finditer(html))
    rows = []
    for m in matches:
        href = html_unescape(m.group(1))  # 網頁原始碼裡 & 常被寫成 &amp;，要先還原才能正確切參數
        qs = href.split("?", 1)[1] if "?" in href else ""
        params = dict(
            (kv.split("=", 1)[0], kv.split("=", 1)[1])
            for kv in qs.split("&")
            if "=" in kv
        )
        fund = params.get("fund")
        date = params.get("date")  # 格式 YYYYMMDD，西元
        if not (fund and date and re.match(r"^\d{8}$", date)):
            continue
        rows.append({
            "fund": fund,
            "date": date,
            "seq": params.get("seq", "1"),
            "type": params.get("type", ""),
            "href": href,
        })

    if not rows:
        # 診斷用：如果完全解析不到任何一列，印出關鍵線索
        print(
            f"[diag] 本頁解析為0列 | html長度={len(html)} | 連結數={len(matches)}",
            file=sys.stderr,
        )
    return rows


def extract_ex_date_and_amount(detail_html: str):
    """從公告內文解析「除息交易日」與「預計/預估/實際配發金額」。
    找不到就回傳 (None, None)，呼叫端要自行略過該筆。
    """
    text = strip_tags(detail_html)
    text = re.sub(r"\s+", "", text)  # 各投信排版不一，斷行/空白都先拿掉再比對

    ex_date = None
    m = EX_DATE_RE.search(text)
    if m:
        roc_year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            ex_date = datetime(roc_year + 1911, month, day).date().isoformat()
        except ValueError:
            ex_date = None

    amount = None
    for am in AMOUNT_RE.finditer(text):
        context = text[max(0, am.start() - 40): am.start()]
        if "配發金額" in context:
            try:
                amount = round(float(am.group(1)), 4)
            except ValueError:
                amount = None
            break

    return ex_date, amount


def fetch_announced_events(etf_codes: set) -> list:
    """掃近一個月的「收益分配」公告，抓出還沒發生、但已經公告的除息日+預估金額。
    這是全市場共用的一份列表，頁數只跟時間範圍有關，跟追蹤幾檔ETF無關。
    單一頁或單一篇公告失敗都只跳過該筆，不影響其他資料。
    """
    cutoff = (datetime.now(TAIPEI_TZ) - timedelta(days=ANNOUNCEMENT_LOOKBACK_DAYS)).strftime("%Y%m%d")
    announced = []
    seen_keys = set()

    for page in range(ANNOUNCEMENT_MAX_PAGES):
        offset = page * 10
        url = ANNOUNCEMENT_LIST_URL.format(offset=offset)
        try:
            html = http_get(url, max_retries=3, base_delay=15)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 公告列表第{page + 1}頁抓取失敗，停止往後翻頁: {e}", file=sys.stderr)
            break

        rows = parse_announcement_rows(html)
        if not rows:
            print(f"[info] 公告列表第{page + 1}頁沒有資料，結束翻頁")
            break

        reached_cutoff = False
        for row in rows:
            if row["date"] < cutoff:
                reached_cutoff = True
                continue
            if row["fund"] not in etf_codes:
                continue
            key = (row["fund"], row["date"], row["seq"])
            if key in seen_keys:
                continue
            seen_keys.add(key)

            detail_url = "https://www.twse.com.tw" + row["href"]
            try:
                detail_html = http_get(detail_url, max_retries=2, base_delay=8)
            except Exception as e:  # noqa: BLE001
                print(f"[warn] 公告內文抓取失敗，略過 {row['fund']} {row['date']}: {e}", file=sys.stderr)
                continue

            ex_date, amount = extract_ex_date_and_amount(detail_html)
            if ex_date and amount is not None:
                announced.append({"code": row["fund"], "ex_date": ex_date, "amount": amount})
            time.sleep(0.3)

        print(f"[info] 公告列表第{page + 1}頁解析出 {len(rows)} 則收益分配公告")
        if reached_cutoff:
            break
        time.sleep(0.3)

    return announced


def main():
    all_etfs = []
    for market in ("TWSE", "TPEx"):
        try:
            all_etfs.extend(fetch_etf_list(market))
        except Exception as e:  # noqa: BLE001
            print(f"[error] 抓取 {market} ETF清單失敗: {e}", file=sys.stderr)

    if not all_etfs:
        print("[fatal] 兩個市場的ETF清單都抓不到，中止", file=sys.stderr)
        sys.exit(1)

    print("[info] 開始抓取每檔ETF的官方分類(證券類別/主題因子)")
    total_for_class = len(all_etfs)
    for idx, etf in enumerate(all_etfs, 1):
        asset_type, tags = fetch_etf_classification(etf["code"])
        etf["category"] = classify_etf(etf["name"], asset_type, tags)
        if idx % 50 == 0 or idx == total_for_class:
            print(f"[info] 分類進度 {idx}/{total_for_class}")
        time.sleep(0.2)

    all_events = []
    meta = {}
    etf_by_code = {}
    actual_keys = set()  # (code, ex_date) 已經有Yahoo實際數字的，公告預估版本要讓路
    total = len(all_etfs)
    for idx, etf in enumerate(all_etfs, 1):
        code, market, name, category = etf["code"], etf["market"], etf["name"], etf["category"]
        etf_by_code[code] = etf
        print(f"[{idx}/{total}] 抓取除息事件 {code} {name} ({market})")
        events = fetch_dividend_events(code, market)
        for ev in events:
            all_events.append({
                "code": code,
                "name": name,
                "market": market,
                "category": category,
                "ex_date": ev["ex_date"],
                "amount": ev["amount"],
                "status": "actual",
            })
            actual_keys.add((code, ev["ex_date"]))
        freq_info = infer_frequency(events)
        meta[code] = {
            "code": code,
            "name": name,
            "market": market,
            "category": category,
            "frequency": freq_info["frequency"],
            "freq_months": freq_info["freq_months"],
        }
        # 對來源站溫和一點，避免被判定為濫用
        time.sleep(0.3)

    print("[info] 開始掃描近一個月的收益分配公告(已公告但尚未除息)")
    try:
        announced = fetch_announced_events(set(etf_by_code.keys()))
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 公告掃描整段失敗，略過這部分，不影響其他資料: {e}", file=sys.stderr)
        announced = []

    added_announced = 0
    for a in announced:
        code = a["code"]
        key = (code, a["ex_date"])
        if key in actual_keys or key in {(e["code"], e["ex_date"]) for e in all_events}:
            continue  # 已經有實際數字了，不需要公告預估版本
        etf = etf_by_code.get(code)
        if not etf:
            continue
        all_events.append({
            "code": code,
            "name": etf["name"],
            "market": etf["market"],
            "category": etf["category"],
            "ex_date": a["ex_date"],
            "amount": a["amount"],
            "status": "announced",
        })
        added_announced += 1
    print(f"[info] 公告預估新增 {added_announced} 筆尚未除息的事件")

    now_iso = datetime.now(TAIPEI_TZ).isoformat()
    out_dividends = {
        "updated_at": now_iso,
        "events": sorted(all_events, key=lambda x: (x["ex_date"], x["code"])),
    }
    out_meta = {
        "updated_at": now_iso,
        "etfs": meta,
    }

    with open("data/dividends.json", "w", encoding="utf-8") as f:
        json.dump(out_dividends, f, ensure_ascii=False, indent=2)
    with open("data/etf_meta.json", "w", encoding="utf-8") as f:
        json.dump(out_meta, f, ensure_ascii=False, indent=2)

    print(f"[done] 共 {len(all_etfs)} 檔ETF、{len(all_events)} 筆除息事件，寫入 data/ 完成")


if __name__ == "__main__":
    main()
