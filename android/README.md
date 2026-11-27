# Knowledge Hub 手機 App

Android 1.0.7 啟動後，在 App 內直接開啟 Knowledge Hub。手機與網頁共用「今天讀什麼」、「接續與理解」和「知識庫」，摺疊螢幕隨視窗寬度調整。文章、導讀、追問與線上筆記都沿用網站的讀者帳號。[開啟 Knowledge Hub](https://momonong.me/knowledge/)

用原生導覽的「今日」、「接續」和「知識庫」切換閱讀區。較窄的視窗使用底列，寬視窗或高度較低的橫向視窗使用側欄。文章中的站內連結會繼續用同一個閱讀器開啟，在文章按 Android 返回鍵可回到上一個網頁。網路中斷時，點「重試連線」可重新開啟該頁。

點導覽列的下載箭頭會開啟 Android 更新彈窗。若下載需要登入，點「在 App 內登入」，完成後回到更新彈窗，再依 Android 提示安裝。舊版裝置有收藏時，彈窗內也可選「匯出舊收藏」，透過 Android 檔案選擇器儲存 HTML。首次安裝可從 Momonong 服務頁的 Knowledge Hub 詳情進入下載點。[服務頁](https://momonong.me/#services) · [Android 下載點](https://momonong.me/knowledge/knowledge-hub.apk)

## 通行密鑰與信任裝置

支援 WebAuthn 的 Android System WebView 會以第一方 App 模式呼叫 Android Credential Manager。讀者從網頁按鈕啟動驗證，密碼登入也可隨時使用。App 進入背景時會寫入網站的持久 Cookie，之後開啟可接續最近成功載入的固定版次文章。站主管理頁從 App 點選時會先確認，再用系統瀏覽器開啟。[Android WebView 登入指南](https://developer.android.com/identity/sign-in/credential-manager-webview)

發行時，將 [Android 發行憑證的 Digital Asset Links 宣告](assetlinks.release-entry.json)合併到 `momonong.me` 根網域的 `/.well-known/assetlinks.json` 陣列，保留該網域已有的所有宣告。如果相同套件、憑證與關聯已存在，沿用該筆。檔案需允許未登入的 HTTPS 請求直接取得，回應 HTTP 200 與 `Content-Type: application/json`。[Digital Asset Links 要求](https://developer.android.com/identity/credential-manager/prerequisites)

通行密鑰驗收需使用與測試網域的 Digital Asset Links 相符的簽署憑證，並在具備螢幕鎖與通行密鑰提供者的 Android 手機上執行。驗收包括註冊、驗證、結束 App 後接續原文章、撤銷信任後重新驗證，以及較舊 WebView 的密碼登入。正式網域的候選宣告僅涵蓋發行憑證。

## 建置與驗證

最低支援 Android 8.0（API 26）。以 JDK 17、Android SDK 37 與 Build Tools 37.0.0 在此目錄執行。

```bash
./gradlew testDebugUnitTest lintRelease assembleDebug
```

從專案根目錄可用 Playwright 與 Chromium 檢查登入、文章與返回動線。

```bash
python3 -B android/checks/site_fixture.py | node android/checks/verify_site_reader.cjs
```

發行 APK 使用既有套件名稱 `com.momonong.knowledgehub`。將原簽署目錄提供給建置腳本以產生可覆蓋更新的 APK。成品和更新資訊位於 `build/knowledge-hub-android.apk` 和 `build/android-release.json`。

```bash
KNOWLEDGE_HUB_SIGNING_DIR=/path/to/private/signing ./build.sh
```
