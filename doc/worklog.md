# 工作日誌

## 2026-10-13
- 建立專案骨架(README、doc/、gridbot/、tests/)。
- 實作網格引擎、回測、資料來源、CLI(backtest / paper)。
- 單元測試發現 bug:初始賣單與「買單成交後補的賣單」會落在同一格而互相覆蓋,持倉多出一份。
  解法:現價所在處留一個空檔,保證任何時刻恰有 `grids` 張單;新增不變量測試。
- 自我 code review:CLI 補上檔案/網路錯誤處理(OSError)。
- 未驗證:Binance 網路路徑(`--symbol` / `paper`)尚未實際連線測試,僅驗證了錯誤處理。
