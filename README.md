# crypto-grid-bot

加密貨幣**現貨網格交易**工具:純 Python 標準庫(無第三方依賴),提供網格引擎、回測與模擬盤。
**目前不支援實盤下單**,也不構成投資建議。

## 技術棧
- Python 3.9+,僅用標準庫(`argparse`、`urllib`、`unittest`)
- 行情來源:Binance 公開 API(免金鑰),或自備 CSV / 合成資料

## 安裝與環境設定
無需安裝依賴:

```bash
cd crypto-grid-bot
python3 -m unittest discover -s tests -t .   # 跑測試
```

## 使用方式

```bash
# 1. 合成隨機漫步資料回測(離線)
python3 -m gridbot backtest --lower 80 --upper 120 --grids 20 --limit 1000 --vol 0.008

# 2. Binance 歷史 K 線回測
python3 -m gridbot backtest --symbol BTCUSDT --interval 1h --limit 1000 \
    --lower 60000 --upper 70000 --grids 25 --invest 1000 --mode geometric

# 3. 自備 CSV(欄位 ts,open,high,low,close)
python3 -m gridbot backtest --csv data.csv --lower 1 --upper 2

# 4. 模擬盤(即時現價,只在本地模擬成交)
python3 -m gridbot paper --symbol BTCUSDT --lower 60000 --upper 70000 --grids 25 --invest 1000
```

共用參數:`--lower/--upper` 區間、`--grids` 格數、`--invest` 本金、`--mode arithmetic|geometric`、
`--fee` 單邊手續費率(預設 0.001)、`--stop-loss` 停損價(須低於 lower,觸發後全數賣出並停機)。

## 運作原理
1. 區間切成 N 格(N+1 條價位線),現價下方掛買單、上方掛賣單,現價所在處留一個空檔。
2. 買單在 level *i* 成交 → 在 *i+1* 掛賣單;賣單成交 → 在 *i-1* 掛買單,賺取格差。
3. 每格數量固定,啟動時依「所有買單 + 初始持倉」配置本金,不會透支。
4. 回測的 K 線內部路徑為保守假設(陽線先低後高、陰線先高後低),不含滑價。

風險:價格跌出區間會被套在持倉中(可用 `--stop-loss` 限制);單邊上漲會賣完而錯過漲幅。
格距須大於來回手續費,否則 CLI 會警告。

## 目錄結構
```
gridbot/
  engine.py    網格引擎(GridConfig / GridEngine)
  backtest.py  K 線回測與績效
  data.py      CSV、Binance 公開 API、合成資料
  cli.py       命令列入口(python3 -m gridbot)
tests/         單元測試
doc/           過程文件
```

## doc/ 文件索引
- [worklog.md](doc/worklog.md) 工作日誌
- [todo.md](doc/todo.md) 待辦清單
- [plan.md](doc/plan.md) 計畫與設計取捨
