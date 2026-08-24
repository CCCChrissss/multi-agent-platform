# 專案目標

這個專案的目標是發展一個**公司內部的多 agent 平台**，理想型態類似 n8n、Dify 這類搭建平台：讓內部員工能在 no-code / low-code 的情況下，自行組裝工作流，且平台必須原生支援 agent（不只是固定流程的自動化）。

**這不是為了特定場景（例如目前的語音轉文字 → 檢核 → 通知）而建立的專案**——目前的 pipeline（見 [README.md](README.md)）只是驗證架構用的範例場景，不是最終目的。

目前的「語音轉文字 → 檢核 → 通知」場景，預計會是第一個正式在這個平台上線的場景，所以現階段開發會以這個場景作為示範、拿來驗證平台的基礎建設是否堪用。但這只是示範場景，開發時不可以被這個場景侷限住——現在專案裡已經有不少元件，是這個場景本身其實用不到、但為了平台的通用性/可擴展性而先建的（例如 MCPGateway 讓 MCP server 可以聚合多個、LiteLLM Gateway 讓 LLM 呼叫走統一介面等）。看到「這個場景用不到」不代表元件多餘，要先確認是不是為了支撐平台的通用能力才存在。

## 對開發的影響

因為目標是通用平台而非單一場景，開發時要優先考慮**可擴展性**，具體來說：

- **AI 基礎建設要素件化、可替換**：像現在的 LLM Gateway（[gateway/](gateway/)）這類元件，設計時要假設未來會有更多模型/provider、更多 workflow 節點類型會用到它，不要綁死在目前這一個 pipeline 的需求上。
- **新增功能前先想「這是平台能力還是這個場景的邏輯」**：平台能力（例如 LLM Gateway、MCP Gateway、service registry）要放在通用的基礎建設層；場景邏輯（例如「有沒有提到台積電」）要留在 workflow/場景層，不要滲透進基礎建設。
- **為 no-code/low-code 搭建鋪路**：目前是用程式碼直接寫 LangGraph pipeline，但長期要支援讓使用者用視覺化/宣告式方式組裝 workflow 與 agent，設計節點、service、gateway 的介面時要考慮未來能否被非工程背景的使用者透過 UI 組裝。

## Harness Engineering 原則

每次要新增或調整 agent、tool 時，先看 [docs/harness-engineering-principles.md](docs/harness-engineering-principles.md)——裡面整理了 agent 需要回饋迴路（工具回傳值、執行中注入、單輪驗收、外層 loop）而非完美提示的核心原則，以及動手前的檢查清單。
