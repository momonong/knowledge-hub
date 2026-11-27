# Knowledge Hub v1.1

每天從公開論文與框架更新選出五篇，在同一頁看懂新方法、核對原文、留下自己的理解。首頁先給閱讀切入點，展開後再看實驗數字與代價。遇到想深挖的文章，點「開始一起讀」即可進入帶讀室。

[Android 手機 App 與建置說明](android/README.md)

## 在本機開始閱讀

使用 Python 3.12，從專案根目錄執行。應用會建立本機 SQLite 資料庫並監聽 loopback。開啟首頁後可點「更新簡報」抓取公開來源。

```bash
mkdir -p .local
python3.12 -B -m knowledge_hub --db .local/knowledge.sqlite3 --port 8765 --quiet
```

[開啟本機首頁](http://127.0.0.1:8765/) · [查看抓取時間、來源與快取狀態](http://127.0.0.1:8765/discovery/status)

要使用真實模型，請將下列端點及模型名稱換成自己已配置的 OpenAI-compatible 服務。如果服務支援嚴格 JSON Schema，可加入 `--primary-structured`。需要認證時，由 `--primary-token-file` 指向本機受保護的憑證檔。

```bash
mkdir -p .local
python3.12 -B -m knowledge_hub --db .local/knowledge.sqlite3 --port 8765 \
  --primary-base-url http://127.0.0.1:11434/v1 --primary-model YOUR_MODEL \
  --brief-model primary --quiet
```

程式執行期間，內建排程於台灣時間 07:00 更新。L1 與 L2 在背景產生，補到同一出版版次的全文後可重算。L3 導讀按文章版本快取，L4 追問逐段串流，只有通過引文 ID 校驗的段落才會顯示。

## 同頁帶讀

- 點左欄的圖表提示，右欄會定位原圖、原段或同版 PDF。窄版可在帶讀與原文間切換。
- 選取原文後可直接「問這段」，或標記 `#想法`、`#盲點/待查`、`#可試看看`。追問若有值得留下的結論，可按「留下這份理解」，缺少原文依據時可轉存待查。
- 展開「這篇連到你庫裡的舊知」可看到具體關聯、摘錄與原紀錄。相關來源來自在同一個庫內匯入的教材、已讀文章和自己的筆記。新庫可以先從今日簡報累積。
- 「相關領域」依文章的標題、摘要與方法辨識 Robotics、World Models、AI Agents、RL、自動駕駛。一篇可以橫跨多個領域。已讀文章、筆記和待查問題有可對照的方法時，帶讀室會附上關聯理由與來源。

同一實例的閱讀狀態保存在資料庫。每台電腦各自建立的本機庫各自保存進度。[v1.1 閱讀實測與五分鐘檢查](docs/v1.1-experience.md)

## 信任手機

在正式網域配置通行金鑰後，首次登入可勾「信任這支裝置 30 天」，輸入原有的 KH 密碼，為裝置命名，再用系統指紋或螢幕鎖定建立通行金鑰。之後開啟 App 可直接續讀，到期時點「用指紋或螢幕鎖定登入」。一般密碼登入仍採閒置 30 分鐘、最長 8 小時的期限。

手機遺失時，在另一台裝置開啟 KH 的「登入與信任裝置」設定頁，用站主管理登入後，對該名稱按「撤銷」。平台同步的同一把通行金鑰也會失效。其他獨立綁定的通行金鑰可繼續使用。

執行 KH 的 Python 環境須安裝 [通行金鑰依賴](deploy/requirements.txt)。Android 使用 Credential Manager，發布時需在正式網域合併對應於 App 發行簽章的 Digital Asset Links 宣告。網站與 Android 由伺服器驗證標準 WebAuthn 回應。瀏覽器回歸測試使用隔離資料庫與 Chromium 虛擬驗證器。[Android WebView 官方說明](https://developer.android.com/identity/sign-in/credential-manager-webview)

## 執行測試

```bash
PYTHONPATH=checks python3.12 -B -m unittest discover -s checks -p 'test_*.py'
```

瀏覽器狀態與 SSE 時序測試會使用 Node.js。PDF 文字及圖像檢查使用 Linux 的 Poppler 與 `prlimit`。瀏覽器端對端腳本位於 `checks/integration/`，可搭配 Playwright 及獨立測試庫執行。
