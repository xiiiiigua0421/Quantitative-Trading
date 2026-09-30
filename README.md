# 美股量化交易程式 v0.1

第一版提供單一美股／ETF 的日線均線策略、可重現回測、公司行動帳務、HTML 報告，以及 Alpaca 模擬交易。從命令列操作；需要 Python 3.11 以上，目前驗證環境為 Python 3.12.14。

## 直接執行

目前專案的 `.venv` 已安裝完成。從專案資料夾執行：

```sh
.venv/bin/qt demo
```

這會建立 `data/demo.csv`，並輸出 `reports/demo/report.html`、`metrics.json`、`equity.csv`、`trades.csv`、`signals.csv`、`manifest.json`。DEMO 是合成行情，只驗證功能；報告內有明確標示。

```sh
.venv/bin/qt data validate --config configs/demo.yaml
.venv/bin/qt backtest --config configs/demo.yaml
.venv/bin/python -m pytest -q
```

在其他電腦重新安裝：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt
.venv/bin/python -m pip install -e . --no-deps
```

`requirements-lock.txt` 保存已測版本。YAML 中的路徑相對於設定檔的位置；命令的 `--input`、`--output` 路徑相對於目前工作目錄。

## 資料與設定

`configs/demo.yaml` 是可直接執行的合成範例；`configs/alpaca.yaml` 用 SPY 示範介面設定，代碼不構成投資建議。可修改成你要研究的標的。

預設工程參數：初始資金 USD 10,000，均線 20／60 日，進場目標為淨值的 50%，進場曝險上限 80%，單筆新增買單含成本上限 USD 5,000，保留淨值 10% 的現金，最大回撤門檻 15%。參數是功能示範，需依實際策略重新設定。

成本使用 `commission_bps`、`slippage_bps` 與 `min_commission`，1 bps = 0.01%。`max_order_notional` 限制新增買單的含成本金額；出場優先全數賣出，因此其金額可能超過進場上限。曝險限制在進場時檢查，後續價格變動可能讓持倉比例改變。

CSV 使用以下欄位，日期是美國交易所交易日，價格為未復權美元價格：

```csv
date,open,high,low,close,volume,split_ratio,dividend,dividend_pay_date
2024-07-01,100,102,99,101,1000,1,0,
2024-07-02,50,52,49,51,2000,2,0.5,2024-07-08
```

`split_ratio` 是新股數／舊股數；2:1 拆股填 2。`dividend` 是除息日每股金額；支付日未知時留空，應收股息保留於淨值但不作為可用現金。新買進的部位不取得當日除息權利。缺少公司行動欄位時採中性預設並揭露完整性限制。

```sh
.venv/bin/qt data import --config configs/alpaca.yaml --input /absolute/path/bars.csv
.venv/bin/qt backtest --config configs/alpaca.yaml
```

核對原始價格、行情來源、區間所有公司行動後，可在匯入時加 `--actions-complete`，才有資格用於模擬送單。此旗標是使用者的資料宣告，不會自動補齊資料。已有 metadata 的資料會檢查代碼、feed 與 SHA-256；復權資料拒絕進入成交帳務。

## Alpaca 資料與模擬交易

使用獨立、初始空倉的 Paper 帳戶。將 `.env.example` 中兩個名稱匯出為環境變數：`APCA_API_KEY_ID`、`APCA_API_SECRET_KEY`；`.env` 檔不會自動載入。金鑰不写入報告或交易狀態。

```sh
.venv/bin/qt data fetch --config configs/alpaca.yaml --start 2024-01-01 --end 2026-09-29
.venv/bin/qt backtest --config configs/alpaca.yaml
.venv/bin/qt paper run --config configs/alpaca.yaml
```

`paper run` 預設只查詢與預覽。準備好模擬帳戶後，可明確啟用 paper 送單與持續更新：

```sh
.venv/bin/qt paper run --config configs/alpaca.yaml --submit --loop --refresh
.venv/bin/qt status --config configs/alpaca.yaml
.venv/bin/qt stop --config configs/alpaca.yaml
.venv/bin/qt resume --config configs/alpaca.yaml
```

此版本的交易主機固定為 `paper-api.alpaca.markets`，沒有實盤模式。下載及模擬交易需要帳戶的行情／公司行動 API 權限；下載包含分頁、raw 日線與拆股／美元現金股息疊加。遇到併購、分拆、股票股息等未支援事件會停止，不會忽略其經濟效果。供應商事件可能延遲，報告保留來源完整性的限制。[行情 API](https://docs.alpaca.markets/us/reference/stockbars)、[公司行動 API](https://docs.alpaca.markets/us/reference/corporateactions-1)

持續模式每 60 秒檢查一次；`--refresh` 在前一個已完成交易日尚未取得時重新下載近一年資料。只在正常交易時段開盤後 15 分鐘內送出前一日訊號，避免錯過開盤後追價。排程依券商日曆與紐約時區，包含休市、提早收盤及夏令時間。主機需持續運行且不休眠。

Paper 帳戶以券商回報的實際模擬現金／淨值 sizing，`initial_cash` 只控制離線回測。只買整股、只做多；以現金限額計算，忽略保證金購買力。持倉符合訊號時不每天再平衡。[官方模擬交易規則](https://docs.alpaca.markets/us/docs/paper-trading)

## 執行狀態與故障處理

- 每個帳戶／標的／交易日最多一個持久化交易意圖；本地檔案鎖、SQLite 與固定 client order ID 共同避免重複送單。
- 送單前保存意圖。逾時或回覆不確定後，狀態為 `unknown`，後續只查詢原訂單，不自動重送。若長期查不到，需要先向 paper 儀表板核對，再人工修復狀態。
- 部分成交、未成交訂單或未解決意圖會阻止新訂單。重啟先核對本地成交總量與券商持倉；外部訂單、手動持倉或不一致的股數都會停止。
- `state/*.sqlite3` 是交易所有權紀錄，需保留與備份。不要刪除它來解除阻擋；若帳戶已有持倉而狀態遺失，程式會要求人工核對。
- `stop` 封鎖所有新送單；不撤銷既有訂單，也不平倉。`resume` 只解除人工停止旗標。
- 回撤風控的停止狀態另行保存：觸發後永久禁止重新進場，已有部位在下一個允許的開盤窗口嘗試平倉。人工 `stop` 優先於自動平倉；`resume` 不清除回撤停止。
- Paper 回撤以執行時抽樣的帳戶淨值檢查；回測則以收盤檢查。行情過期、時鐘偏差或買力不足會阻止新曝險，市價單仍可能因跳空超出估計成本。

模擬持倉因拆股產生的股數變化，目前會要求人工核對後恢復，不會自動將差異當成成交。監控透過命令列 JSON 與持久狀態提供；外部通知、自动撤單、管理介面和實盤串接是後續功能。

## 回測口徑與策略驗證

收盤完成後，快均線高於慢均線代表持有；低於或等於時代表空倉。只在空倉進場、持倉時出場。暖機期間不交易；訊號在下一個有資料且有成交量的交易日開盤成交，加上不利方向滑價與手續費。期末不強制平倉，最後一日訊號不會憑空成交。

拆股按當時生效比例調整持倉與均線歷史，不改寫過去已發布訊號。股息在除息日列應收，支付日或其後第一個有資料的交易日才入現金；未支付股息包含於期末淨值。反向拆股产生的零碎權益在回測保留為股數，尚未模擬券商的畸零股現金結算。

基準在暖機後第一個可成交開盤，用全部可用初始現金買入持有，使用相同成本與公司行動。策略只投入設定比例，因此其報酬／風險與滿倉基準的差異需同時評估曝險。年化採 252 個交易日、Sharpe 無風險利率為 0；定義保存在報告內。

正式評估時，先按時間保留未參與選參數的測試區間。參數確定後再執行該區間，並提高成本重跑；範例：

```sh
.venv/bin/qt backtest --config configs/alpaca.yaml --end 2024-12-31 --output reports/development
.venv/bin/qt backtest --config configs/alpaca.yaml --start 2025-01-01 --end 2025-12-31 --output reports/holdout
```

每個區間會各自暖機；比較時使用報告的 `evaluation_start`。這個指令不會自動防止你反覆查看測試集或挑選結果，所有調參需保留實驗紀錄。第一版尚未提供自動 walk-forward、參數搜尋與多標的組合帳務。

## 目錄

```text
configs/                 範例設定
data/demo.csv            合成行情與來源 metadata
src/quant_trading/       資料、策略、回測、風控、paper 執行與 CLI
tests/                   帳務、資料、訂單恢復與安全性測試
reports/                 HTML、CSV、JSON 報告（執行產生）
state/                   模擬狀態資料庫（執行產生）
PLAN.md                  原始開發計劃
```

已完成離線範例與 mock broker 驗證。真實行情品質、帳戶權限及外部 Paper API 的實際連線／成交，需使用你的帳戶做前向驗證；程式未在開發過程送出外部訂單。
