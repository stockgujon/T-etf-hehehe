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
  2. 除息事件：Yahoo Finance chart API 的 dividends 事件
     上市 ETF 用 {代號}.TW，上櫃 ETF 用 {代號}.TWO

這兩個都不是官方「文件化」的公開API，是社群長期驗證可用的公開端點；
若哪天格式跑掉，屬於預期中會需要調整的維護點，程式已盡量寫得寬容
(找不到欄位就跳過該筆，不會讓整個流程死掉)。

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
from html.parser import HTMLParser

TAIPEI_TZ = timezone(timedelta(hours=8))

ISIN_URLS = {
    "TWSE": "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2",
    "TPEx": "https://isin.twse.com.tw/isin/C_public.jsp?strMode=4",
}

# 用來粗略判斷ETF類別的關鍵字(只需要「大概」，之後可再微調)
LEVERAGE_INVERSE_HINTS = ["正2", "正二", "反1", "反一", "槓桿", "2X", "反向"]
BOND_HINTS = ["债", "債", "公司債", "美債", "公債", "投等債", "高收債"]
COMMODITY_HINTS = ["原油", "黃金", "黄金", "白銀", "商品", "期货", "期貨", "石油"]

USER_AGENT = "Mozilla/5.0 (compatible; tw-etf-dividend-tracker/1.0)"


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
    for kw in LEVERAGE_INVERSE_HINTS:
        if kw in name:
            return "槓桿反向"
    for kw in BOND_HINTS:
        if kw in name:
            return "債券型"
    for kw in COMMODITY_HINTS:
        if kw in name:
            return "商品期貨型"
    return "一般股票型"


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

    all_events = []
    meta = {}
    total = len(all_etfs)
    for idx, etf in enumerate(all_etfs, 1):
        code, market, name, category = etf["code"], etf["market"], etf["name"], etf["category"]
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
            })
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
