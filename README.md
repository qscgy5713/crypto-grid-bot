# crypto-grid-bot

加密貨幣**現貨網格交易**工具:純 Python 標準庫(無第三方依賴),提供網格引擎、回測與模擬盤。
**目前不支援實盤下單**,也不構成投資建議。

## 技術棧
- Python 3.9+,僅用標準庫(`argparse`、`urllib`、`unittest`)
- 行情來源:Binance 公開 REST / WebSocket(免金鑰),或自備 CSV / 合成資料

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
    --lower 75000 --upper 92000 --grids 25 --invest 1000 --mode geometric

# 3. 自備 CSV(欄位 ts,open,high,low,close)
python3 -m gridbot backtest --csv data.csv --lower 1 --upper 2

# 4. 參數掃描(格數 × 區間 × 模式,前 70% 樣本內 / 後 30% 樣本外)
python3 -m gridbot sweep --symbol BTCUSDT --limit 1000 --grids-list 10,20,40 --range-pcts 5,10,20 --top 10

# 5. 滾動視窗驗證(訓練段挑參數 → 套到緊接的測試段;--limit>1000 自動分頁)
python3 -m gridbot walkforward --symbol BTCUSDT --limit 4000 --train 720 --test 240

# (sweep / walkforward 未指定 --symbol 與 --csv 時預設 BTCUSDT)

# 6. 模擬盤(即時現價,只在本地模擬成交;區間必須包住現價,範例區間請依當下價格調整)
#    狀態每次輪詢後存到 state/paper_<SYMBOL>.json,重跑相同指令會自動接續;
#    價格預設走 WebSocket 即時成交價(--source ws,斷線自動重連,連不上自動改輪詢,每 60 秒再試著回到 WebSocket);
#    --source poll --interval N 改為每 N 秒輪詢一次 REST 現價
#    斷線防護:超過 --stale-warn 秒(預設 15)沒收到價格就警告並響鈴,超過 --stale-halt 秒(預設 300,0=不停機)
#    就存檔並自動停機(退出碼 3,重跑同一指令可接續);--alert-cmd 可掛通知指令(見下方「斷線警報」)
#    參數不同須加 --reset 重新開始,或用 --state 指定其他檔案;--max-polls N 限制處理的價格筆數
python3 -m gridbot paper --symbol BTCUSDT --lower 75000 --upper 92000 --grids 25 --invest 1000
```

共用參數:`--lower/--upper` 區間、`--grids` 格數、`--invest` 本金、`--mode arithmetic|geometric`、
`--rank-by in|out`(sweep 排序依據,預設樣本內)、`--fee` 單邊手續費率(預設 0.001)、`--stop-loss` 停損價(須低於 lower,觸發後全數賣出並停機)。

## 斷線警報與自動停機(paper)
- 價格來源沒資料時每秒送心跳,主迴圈據此計算「已幾秒沒收到價格」。WebSocket 連線若 30 秒完全沒資料(連 Binance 的 ping 也沒有)會主動重連。
- 警告:每 60 秒重複提醒一次;價格恢復時顯示中斷秒數。停機:存檔後以退出碼 3 結束,**不是**停損,重跑同一指令即可接續。
- `--alert-cmd` 會在 warn / halt / recover 時執行,訊息在環境變數 `GRIDBOT_ALERT_MSG`、等級在 `GRIDBOT_ALERT_LEVEL`。macOS 通知範例:
  ```bash
  --alert-cmd 'osascript -e "display notification \"$GRIDBOT_ALERT_MSG\" with title \"gridbot\""'
  ```
- `--interval`(輪詢間隔)必須小於 `--stale-warn`,否則輪詢本身就會觸發斷線警報;同一個狀態檔同時只允許一個 paper 行程(flock 鎖檔),Ctrl+C / SIGTERM 都會先存檔再結束。
- 注意:筆電休眠後喚醒,舊連線已死,可能直接觸發自動停機,重跑即可。

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
  sweep.py     參數掃描、滾動視窗驗證
  state.py     模擬盤狀態存檔(原子寫入)
  watchdog.py  斷線監看(警告 / 自動停機 / 恢復)
  ws.py        精簡 WebSocket 客戶端(僅標準庫)
  data.py      CSV、Binance 公開 API、合成資料
  cli.py       命令列入口(python3 -m gridbot)
tests/         單元測試
state/         模擬盤狀態檔(執行時產生,已 gitignore)
doc/           過程文件
```

## doc/ 文件索引
- [worklog.md](doc/worklog.md) 工作日誌
- [todo.md](doc/todo.md) 待辦清單
- [plan.md](doc/plan.md) 計畫與設計取捨
