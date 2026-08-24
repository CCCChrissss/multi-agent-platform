# 實作計劃:統一 MCP Gateway + 混合式 RBAC

> 狀態:Phase 1 已實作（policy 邏輯已單元驗證;pipeline 端到端待實跑）
> 範圍:新增 2 檔（policy.yaml、policy.py）、改 5 檔（gateway、stt_agent、tsmc_judge、notified/agent、simple_pipeline）、留 4 個 `*/client.py` 待清

## 1. 背景與動機

目前每個 agent node 各自 `new` 一個 [`MCPGateway`](../mcp_servers/gateway.py)，建構子傳進去的 client dict 就是那個 agent 的權限（見 [`workflows/simple_pipeline.py`](../workflows/simple_pipeline.py) L80-81）:

```python
check_gateway    = MCPGateway({"lookup":   LookupMCPClient()})
notified_gateway = MCPGateway({"notified": NotifiedMCPClient()})
```

這帶來兩個問題:

1. **權限模型 = 接線的程式碼**。「哪個 agent 能用哪些工具」寫死在 Python dict 字面值，與 [CLAUDE.md](../CLAUDE.md) 的 no-code / UI 組裝目標衝突——在 UI 改權限等於改原始碼。
2. **連線被複製**。每個 gateway 各自啟動自己的 MCP server stdio 子行程,同一台 server 被多個 agent 用時會重複啟動。

本計劃把 gateway 從「每 agent 一個」改成「**全平台共用一個**」,並用**宣告式 policy**（混合式 RBAC）取代建構子注入來控管權限。這屬於 CLAUDE.md 定義的「平台層能力」,非場景邏輯。

## 2. 名詞與模型

- **principal**:權限主體,對應一個 agent / workflow node（如 `check`、`notified`）。
- **role**:可重用的工具束,把常見的一組工具授權集中定義一次。
- **混合式 RBAC**:principal 可以「掛 role」重用,也可以直接 `allow` 客製,或掛 role 後用 `deny` 覆寫。等價於「per-principal scope + 一層可選的可重用中間束」。

對應傳統 RBAC:

```
傳統 RBAC:   user      → role → permission (對 resource)
本平台:      principal → role → 允許的工具  (對 MCP server 上的 tool)
             (agent/node)
```

**權限顆粒度到工具層級**,`server__*` 只是「整台放行」的萬用字元特例。理由:同一台 server 上可能同時有安全工具（`notified__send_gmail`）與危險工具（`notified__delete_all`）,必須能分開授權。

## 3. 設計決策

| # | 決策 | 理由 |
|---|------|------|
| D1 | 權限顆粒度到工具層級,server 為萬用字元特例 | 同台 server 內需區分安全/危險工具 |
| D2 | **deny 勝過 allow** | 標準做法,可預期 |
| D3 | **雙重把關、fail-closed**:`list_tools` 過濾 + `call_tool` 再驗一次 | list 是 UX,call 是安全邊界;共用後失去「沒連線就呼不到」的物理保證,須由 policy 補回 |
| D4 | principal 用**顯式參數**傳,不靠 contextvar 隱式讀 | policy 是安全輸入,顯式利於測試與稽核 |
| D5 | server 啟動由 **policy.yaml 宣告驅動**,gateway 統一啟動一次 | 「共用」的實體;新增 server 變成改設定而非寫程式 |
| D6 | 未知 principal → 空權限集合（非全放行） | fail-closed |
| D7 | 並行安全**這階段不做**,但介面預留 | 現有 pipeline 為串行,先驗等價,不過度工程 |

## 4. 檔案層級改動

### 4.1 新增 `mcp_servers/policy.yaml`

唯一真相,未來由 UI 產生:

```yaml
servers:                       # gateway 統一啟動
  stt:      {module: mcp_servers.stt.server}
  format:   {module: mcp_servers.format_check.server}
  lookup:   {module: mcp_servers.lookup.server}
  notified: {module: mcp_servers.notified.server}

roles:                         # 可重用的工具束
  transcriber: {allow: ["stt__*", "format__*"]}
  reader:      {allow: ["lookup__*"]}
  notifier:    {allow: ["notified__*"]}

principals:                    # 混合式:掛 role + 可覆寫
  stt:      {roles: [transcriber]}
  check:    {roles: [reader]}
  notified: {roles: [notifier]}
  # 例:完全客製、不走 role
  # some_agent: {allow: ["lookup__query_company_profile"]}
  # 例:掛 role 但扣掉一個危險工具
  # other_agent: {roles: [notifier], deny: ["notified__send_slack_message"]}
```

### 4.2 新增 `mcp_servers/policy.py`（純函式、易測)

無 I/O 以外的副作用,可獨立單元測試:

- `load_policy(path) -> Policy`:解析 yaml 成 dataclass。
- `resolve_allowed(policy, principal) -> Permissions`:展開 `roles`,回傳
  `Permissions(allow, deny)` **兩組 pattern 分開存**。
- `is_allowed(perms, tool_name) -> bool`:命中 `deny` → False;否則命中 `allow` → True。
- **未知 principal → 空的 Permissions**（D6）。

> **關鍵:deny 必須在「比對時」生效,不能用集合減法先扣掉。**
> 因為 `allow` 裡放的是萬用字元 `notified__*`,而 deny 的是具體
> `notified__send_slack_message`——兩者是不同字串,`{notified__*} - {notified__send_slack_message}`
> 減不掉任何東西。所以 allow/deny 要各自留著,比對時「先看 deny 再看 allow」,deny 才真的能覆寫 allow 萬用字元。

解析語意範例:

```
principal other_agent = {roles: [notifier], deny: ["notified__send_slack_message"]}
  allow = {notified__*}   (展開 notifier)
  deny  = {notified__send_slack_message}
  is_allowed("notified__send_gmail_message"): 不命中 deny、命中 allow 萬用字元 → True
  is_allowed("notified__send_slack_message"): 命中 deny → False   # deny 勝出 (D2)
```

### 4.3 改寫 `mcp_servers/gateway.py`

從「收 client dict」改成「讀 policy、統一啟動所有 server、依 principal 過濾/把關」:

```python
class MCPGateway:
    def __init__(self, policy: Policy) -> None:
        self._policy = policy
        self._clients: dict[str, MCPClient] = {}
        self._stack = AsyncExitStack()

    async def connect(self) -> None:
        for name, spec in self._policy.servers.items():
            client = MCPClient(StdioServerParameters(
                command="uv", args=["run", "python", "-m", spec.module]))
            await self._stack.enter_async_context(client)
            self._clients[name] = client

    async def list_openai_tools(self, principal: str) -> list[dict]:
        allow = resolve_allowed(self._policy, principal)
        # 匯集所有 server 的工具 → namespace 成 server__tool → 只回傳 is_allowed 的

    async def call_tool(self, principal: str, tool_name: str, arguments: dict) -> tuple[str, bool]:
        if not is_allowed(resolve_allowed(self._policy, principal), tool_name):
            return f"error: '{principal}' not permitted to call '{tool_name}'", True   # fail-closed (D3)
        # 通過才 partition namespace → route 到對應 client → log_call
```

`log_call` 稽核 choke point 維持不變。`connect/close/__aenter__/__aexit__` 形狀保留。

### 4.4 改 consumer(各多傳一個 principal，D4)

- [`llm/stt_agent.py`](../llm/stt_agent.py):`transcribe(gateway, audio_path, principal)`,內部 gateway 呼叫帶上 principal。
- [`llm/tsmc_judge.py`](../llm/tsmc_judge.py):`mentions_tsmc(gateway, text, principal)`,內部兩處 gateway 呼叫帶上 principal。
- [`mcp_servers/notified/agent.py`](../mcp_servers/notified/agent.py):`decide_and_notify(gateway, ..., principal)` 同理。

### 4.5 改 `workflows/simple_pipeline.py`

核心變化——兩個 gateway 併成一個共用:

```python
gateway = MCPGateway(load_policy("mcp_servers/policy.yaml"))
async with gateway, get_checkpointer() as checkpointer:
    ...
# check_node:    await llm_mentions_tsmc(gateway, transcript, principal="check")
# notified_node: await decide_and_notify(gateway, ..., principal="notified")
```

`build_graph` 從收兩個 gateway 改成收一個。principal 字串沿用各 node 已 set 的 `current_node_name`（`"check"` / `"notified"`）。

### 4.6 收尾(暫留待清)

四個 `mcp_servers/*/client.py`（`stt`、`format_check`、`lookup`、`notified`）的 `SERVER_PARAMS` 子類,gateway 改由 yaml 驅動後不再被 pipeline 使用。**這階段先留著不刪**,確認新路徑等價後再清,避免一次動太多。

## 5. 驗證方式(等價性優先)

現有 pipeline 為 stt→check→notified 串行,改完須驗:

1. **正常路徑等價**:跑 `samples/gen_tsmc_01.wav`,`check` 只看得到 `lookup__*`、`notified` 只看得到 `notified__*`,最終結果與現在一致。
2. **fail-closed 有效**:暫時拿掉 `check` 的 role → `list_tools` 回空、硬呼叫 `call_tool` 也被擋且不 crash(回 `is_error=True`)。
3. **deny 覆寫有效**:某 principal 掛 role 再 deny 一個工具,確認該工具從 list 消失且 call 被擋。
4. **policy.py 單元測試**:`resolve_allowed` / `is_allowed` 對 role 展開、deny 勝出、未知 principal、萬用字元四種情形。

## 6. 分階段

- **Phase 1(本計劃)**:單一共用 gateway + policy.yaml + 混合式解析 + 雙重把關,在現有串行 pipeline 驗等價。
- **Phase 2(之後,不現在做)**:並行安全。共用後同一 `ClientSession` 會被多 node 同時打,屆時每台 server 需 per-call session 或鎖/連線池。介面(`call_tool` 已帶 principal、無共享可變狀態)先預留,不影響 Phase 1。
- **Phase 3(更遠)**:policy.yaml 改由 UI 產生/編輯;principal 從「node 名」升級成「workflow × node 的合成 id」以支援多 workflow 並存。

## 7. 風險與取捨

- **安全邊界從程式碼移到 policy engine**:共用後失去「沒連線就呼不到」的物理保證,改由 policy 正確性 + fail-closed 保障。policy.py 必須有單元測試覆蓋(見 §5.4)。
- **並行**:Phase 1 僅在串行 pipeline 安全,平台化前務必補 Phase 2,否則多 workflow 並行會踩同一 session。
- **principal 命名**:Phase 1 用 node 名當 principal 夠用,但多 workflow 時 node 名會撞;Phase 3 需升級為合成 id。
