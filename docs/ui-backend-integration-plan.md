# UI 與後端整合：現況與設計邊界

這份文件依目前已落地的程式碼重建整合脈絡。原始規劃文件沒有隨上游內容完整保留，
因此本文件描述的是 repository 目前可驗證的行為，不把歷史假設當成已實作功能。

## 1. 目標

Demo UI 必須讀寫平台既有的宣告式來源，而不是另建一套只給前端使用的資料模型：

- workflow/agent 規格：[workflows/definitions/](../workflows/definitions/)
- tool 與 memory 授權：[mcp_servers/policy.yaml](../mcp_servers/policy.yaml)
- 執行端：[agents/runtime.py](../agents/runtime.py)
- Demo API 與前端：[demo/api.py](../demo/api.py)、[demo/index.html](../demo/index.html)

## 2. P0：工具目錄與最小可測能力

[mcp_servers/policy.yaml](../mcp_servers/policy.yaml) 是工具授權的唯一來源；
[mcp_servers/calc/server.py](../mcp_servers/calc/server.py) 提供建立新 agent 後可立即測試的
實際工具。UI 顯示的工具清單由 [demo/api.py](../demo/api.py) 讀取 policy 產生，不在
HTML 內維護第二份固定清單。

## 3. P1：執行時熱載入

[agents/live_spec.py](../agents/live_spec.py) 監看主 workflow、UI 建立的單 agent YAML 與
policy 檔案 mtime，驗證成功後才原子替換 immutable snapshot。
[agents/lifespan.py](../agents/lifespan.py) 在第一次使用 agent 時建立並快取其
`MCPGateway`；policy 更新時同步更新既有 gateway。主 workflow 的 gateway 仍會在啟動
時 eager connect，避免第一個正式 run 才支付全部啟動成本。

載入新版本失敗時必須保留上一份有效 snapshot。這個 fallback 是執行穩定性邊界，
不能改成「解析失敗也部分套用」。

## 4. P2：規格寫入層

[demo/spec_writer.py](../demo/spec_writer.py) 負責更新 step、建立/刪除單 agent 定義，以及
修改 tool/memory 授權。它使用 `ruamel.yaml` 保留 policy 的治理註解，先寫 temporary
file、重新載入驗證，再原子取代正式檔案。

UI 建立的 agent 使用 `workflows/definitions/agent_*.yaml`，由 [.gitignore](../.gitignore)
排除。手寫主 workflow 與 UI 產物因此有明確邊界。任何新寫入功能都應走
`spec_writer`，不可直接由 API handler 拼 YAML。

## 5. P3：Demo API

[demo/api.py](../demo/api.py) 提供以下介面：

- 查詢 catalog、workflow 與 step 設定。
- 更新 model、prompt、tools、memory 開關。
- 建立、刪除及單獨測試 agent。
- 觸發 workflow 並查詢 run 狀態。
- 瀏覽 agent 可讀的 semantic/episodic/procedural memory。
- 執行記憶蒸餾與候選規則審核；細節見 [distill-ui-plan.md](distill-ui-plan.md)。

API 回傳內容必須重新經過正式 loader，確保 UI 顯示的是平台實際可載入的狀態。

## 6. 前端狀態與互動契約

[demo/index.html](../demo/index.html) 是單頁 demo，沒有前端框架或第二套持久化 store。
畫面以 API 回傳的 workflow/agent 為 master state，再保存尚未送出的表單編輯狀態。

- Agent 卡片的 model、prompt、工具與 memory 開關必須對應同一個 step。
- 新增 agent 後要重新抓 workflow/catalog，不能只在瀏覽器內假造成功狀態。
- 刪除、更新或測試失敗時要保留使用者輸入並顯示後端錯誤。
- 工具建議只是 UI 輔助；真正能否呼叫仍由 policy fail-closed 判定。
- 音檔樣本必須依目前 workflow 過濾，避免用錯場景造成假性失敗。

## 7. 驗證

依修改範圍執行：

```powershell
uv run python -m demo.spec_writer_smoke_test
uv run python -m agents.live_spec_smoke_test
```

記憶審核 API 需要 PostgreSQL，另執行：

```powershell
uv run python -m demo.distill_api_smoke_test
```

若 repository 中的實際 smoke test 名稱或前置條件改變，應同步更新本文件與
[testing.md](testing.md)，不得只修改註解中的命令。
