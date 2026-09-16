# 台股ETF除息追蹤

追蹤「國內掛牌ETF（上市+上櫃，含一般股票型、槓桿反向、債券型、商品期貨型）」的除息資訊：

1. **近期除息行事曆**：本月＋下月自動捲動，月底前提前接上下下個月，格內用色塊代號＋點擊看詳情。
2. **全年除息月份總表**：所有ETF對應到通常除息的月份（大致估算，非精算）。

資料每天由 GitHub Actions 自動抓取一次並寫回 `data/` 目錄，網頁本身是純靜態頁面（GitHub Pages 即可）。

## 部署步驟

1. 建一個新的 GitHub repo，把這個資料夾整個推上去。
2. 到 repo 的 **Settings → Actions → General → Workflow permissions**，選擇
   「**Read and write permissions**」並儲存。
   （這一步一定要做，否則排程抓完資料後會沒有權限 `git push` 回 repo。）
3. 到 **Settings → Pages**，Source 選 `Deploy from a branch`，branch 選
   `main` 、資料夾選 `/(root)`，儲存後會得到一個 `https://<你的帳號>.github.io/<repo名稱>/` 網址。
4. 到 **Actions** 分頁，手動觸發一次 `每日更新ETF除息資料`（workflow_dispatch），
   確認能成功跑完、且 `data/dividends.json`、`data/etf_meta.json` 有被更新並commit。
   之後就會照 `.github/workflows/update-data.yml` 裡設定的時間（台灣時間每天 18:30）自動跑。

## 抓取邏輯與已知限制（很重要，請先讀過）

- **ETF清單**：抓自 TWSE 的公開ISIN查詢頁（上市 + 上櫃），用「產業別=ETF」或代號格式(00xxx)判斷。
- **除息事件**：抓自 Yahoo Finance 每檔ETF的股利事件（因為證交所/櫃買中心沒有一個乾淨、涵蓋全部ETF的除息日+金額API，這是目前普遍可行的公開替代方案）。
- **配息頻率／全年月份**：用近12～18個月實際除息次數回推「月配／雙月配／季配／半年配／年配」，全年總表的月份也是取這段期間觀察到的月份，**不是**每檔都對投信公告逐一核對過。你提到「大概抓就好，之後再慢慢調整」就是指這一塊。
- **ETF類型（一般股票型/槓桿反向/債券型/商品期貨型）**：用ETF名稱關鍵字粗略判斷（例如名稱有「正2」「反1」歸槓桿反向），不是官方分類，可能有誤判，之後可以直接改 `scripts/fetch_data.py` 裡的 `LEVERAGE_INVERSE_HINTS` / `BOND_HINTS` / `COMMODITY_HINTS` 關鍵字清單來修正。
- 這兩個資料源都是「社群長期驗證可用、但非正式文件化」的公開端點，如果哪天格式跑掉，`fetch_data.py` 會單檔跳過失敗的ETF、不會讓整個流程掛掉；如果連清單頁本身都抓不到，腳本會直接失敗，交給 workflow 的重試機制（失敗會等5分鐘再試，最多3次；三次都失敗就等下一個排程時間再試一次）。

## 之後想調整的地方

- 想改配息頻率的判斷門檻：改 `scripts/fetch_data.py` 裡 `infer_frequency()` 的 `per_year` 判斷式。
- 想改行事曆一天最多顯示幾個代號：改 `assets/app.js` 裡 `dayEvents.slice(0, 3)` 的數字。
- 想改抓取時間：改 `.github/workflows/update-data.yml` 裡的 `cron`（是UTC時間，台灣時間要-8小時換算）。
