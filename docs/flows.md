# Agent App Server 操作流程

本文件描述目前 Web console 的 Project／Files／Thread 操作、Turn 執行與 SSE reconnect 行為。元件與狀態邊界請見[系統架構](architecture.md)。

## 初次載入與建立 Project

Browser 初始化時會並行讀取 Project、UI preferences 與 models。只有保存的 Project 仍存在時才恢復選擇；無效 preference 會被清除，不會自動選取第一個 Project。

```mermaid
flowchart TD
    load["Browser 載入 Web console"] --> parallel["並行取得<br/>GET /api/projects<br/>GET /api/preferences<br/>GET /api/codex/models"]
    parallel --> valid{"保存的 project_key<br/>仍存在？"}
    valid -->|是| threads["載入該 Project 的 Thread list"]
    threads --> saved{"有保存的 thread_id？"}
    saved -->|是| authorize["讀取並驗證 Thread"]
    authorize --> restored{"Thread 屬於目前 Project？"}
    restored -->|是| select["選取 Thread、連線 SSE、更新 partials"]
    restored -->|否| clear["清除失效的 Thread preference"]
    saved -->|否| choose["等待使用者建立或選擇 session"]
    valid -->|否| reset["清除失效 preferences"]
    reset --> choose_project["顯示所有可選 Project"]
    clear --> choose

    choose_project --> action{"使用者動作"}
    action -->|選既有 Project| save_existing["保存 project_key 並載入 Thread list"]
    save_existing --> choose
    action -->|建立 Project| create["POST /api/projects<br/>只傳單一目錄名稱"]
    create --> validate{"名稱合法且目錄不存在？"}
    validate -->|否| project_error["400 / 409 / 503"]
    validate -->|是| mkdir["在 codex_projects_root 建立目錄<br/>refresh registry"]
    mkdir --> save_project["保存新 Project 的 project_key"]
    save_project --> new_thread["自動 POST /api/codex/threads"]
    new_thread --> select
```

建立 Thread 後，如果 Codex `thread_list` 尚未立即列出它，`CodexService` 會暫存在 pending map，讓 timeline、inspector、composer 與 SSE authorization 仍可立即使用。Thread 出現在正式 list 後，pending entry 會被移除。

## 瀏覽與修改 Project files

Files 分頁只使用目前選定的 `project_key`。Browser 不提交 server absolute path；每個 request 都由 registry 重新取得 Project，再由 `ProjectFileManager` 解析 project-relative path。

```mermaid
flowchart TD
    open["開啟 Files 分頁"] --> list["GET /api/projects/{key}/files<br/>path = 空字串"]
    list --> validate["ProjectFileManager<br/>逐層 lstat + path validation"]
    validate --> tree["回傳 directories-first 的一層 entries"]
    tree --> action{"使用者操作"}

    action -->|展開資料夾| child["lazy GET files?path=relative/path"]
    action -->|上傳| upload["POST files/upload<br/>raw body + name + overwrite"]
    action -->|新增資料夾| mkdir["POST files/directories"]
    action -->|下載檔案| download["GET files/download"]
    action -->|重新命名| rename["PATCH files"]
    action -->|刪除| confirm["Browser confirmation"]
    confirm --> delete["DELETE files<br/>資料夾為 recursive delete"]

    child --> validate
    upload --> validate
    mkdir --> validate
    download --> validate
    rename --> validate
    delete --> validate

    validate -->|absolute / traversal / symlink / invalid type| error["400 / 403 / 404 / 409"]
    upload -->|同名且 overwrite=false| conflict["409 file_exists<br/>Browser 再要求確認"]
```

Directory listing 不顯示 symbolic link 或 special file；後續直接指定這些 path 也會被拒絕。上傳先寫入目標資料夾內的 temporary file，flush／fsync 後再放置到最終名稱。未確認 overwrite 時，同名檔案不會被改動。

## 啟動與串流一個 Turn

Browser 先建立目前 Thread 的 SSE，收到 `console.stream.ready` 後才允許送出新 Turn。模型選單會使用 `model/list` 回傳的 `default_reasoning_effort` 與 `supported_reasoning_efforts`，只列出目前模型支援的 reasoning 等級；未明確選擇時沿用模型預設值，`max`／`ultra` 會提示較快消耗 usage limits。後端在單一 async lock 內先保留 `starting` 狀態，避免兩個同時送達的 request 都通過檢查。

```mermaid
sequenceDiagram
    actor User
    participant Browser
    participant API as FastAPI routes / SSE
    participant Service as CodexService
    participant Turns as TurnManager
    participant Codex as AsyncCodex / app-server
    participant Pump as event-pump task
    participant Hub as EventHub

    Browser->>API: GET /threads/{id}/events
    API->>Service: read_thread(include_turns=false)
    Service->>Codex: list / resume / read
    Service->>Service: 核對 Thread id 與 CWD
    API->>Hub: subscribe(thread_id)
    API-->>Browser: console.stream.ready

    User->>Browser: 送出 prompt
    Browser->>Browser: 立即加入 optimistic user message
    Browser->>API: POST /threads/{id}/turns<br/>{prompt, model, reasoning_effort}
    API->>Service: start_turn()
    Service->>Turns: reserve(thread_id)
    Turns-->>Service: 狀態 = starting
    Service->>Hub: publish console.turn.starting
    Hub-->>API: subscriber queue event
    API-->>Browser: SSE starting

    Service->>Codex: authorize Thread、thread.turn(prompt, model, effort)
    Codex-->>Service: Turn handle
    Service->>Turns: mark_running + attach_task
    Service->>Pump: create_task(handle.stream)
    Service->>Hub: publish console.turn.running
    Service-->>API: accepted + turn_id
    API-->>Browser: HTTP 202

    loop 每個 Codex notification
        Codex-->>Pump: notification
        Pump->>Hub: normalized event
        Hub-->>API: subscriber queue event
        API-->>Browser: SSE event
        Browser->>Browser: 更新 plan、diff、usage、timeline
    end

    Codex-->>Pump: stream 完成或失敗
    opt stream 失敗
        Pump->>Hub: console.turn.error
    end
    Pump->>Turns: finish(thread_id, turn_id)
    Pump->>Hub: console.turn.idle
    Hub-->>API: subscriber queue event
    API-->>Browser: SSE idle
    Browser->>API: 重新取得 authoritative Thread partials
    Browser->>Browser: partial 更新成功後移除該 Turn 的 optimistic items
```

使用者訊息、agent delta 與完成的 tool results 共用依到達順序排列的 live timeline。Browser 在 HTTP request 前先顯示使用者訊息；POST／steer 失敗時移除該 optimistic item 並還原 composer。收到 `item/completed` 時，command、file change、MCP／dynamic tool 等最終 item 會立即顯示為 tool-card，不需等待整個 Turn idle。新的 agent segment 或 tool-card 開始時，前一段會結束 `streaming`／`aria-busy` 狀態，只有目前輸出的最後一段保留閃爍游標。成功時用回傳或 SSE 的 `turn_id` 綁定 item，等 idle 後 authoritative partial 更新成功才清除，避免重複或 refresh 失敗時遺失訊息。

同一 Thread 已是 `starting`、`running`、`stopping` 或正在執行互斥 mutation 時，新 Turn 會回 `409`。不同 Threads 使用不同 active entry，因此可並行。活動 Turn 上再次送出 prompt 會走 steer；steer 也會先加入 optimistic user message，並讓後續 agent delta 建立在它之後。Interrupt 會先呼叫 handle，再發布 `console.turn.stopping`。

## Long-running Goal

Browser 可由 Inspector 表單或 composer `/goal` 指令操作 Goal。所有 Goal endpoint 都先做相同的 Thread/CWD allow-list 驗證；Goal snapshot 直接讀取 Codex Thread store，不寫入 SQLite。

```mermaid
sequenceDiagram
    participant Browser
    participant API as FastAPI
    participant Service as CodexService
    participant Turns as TurnManager
    participant Goal as CodexGoalAdapter
    participant Codex as Codex app-server
    participant Hub as EventHub

    Browser->>API: POST /threads/{id}/goal
    API->>Service: start_goal(objective, token_budget, model, reasoning_effort)
    Service->>Turns: reserve(kind=goal, model=model, reasoning_effort=effort)
    Service->>Codex: thread resume(model=model, config.model_reasoning_effort=effort)
    Service->>Goal: start logical Goal
    Goal->>Codex: goal clear + set active
    Codex-->>Goal: first physical Turn started
    Goal-->>Service: logical handle + Goal snapshot
    Service->>Turns: mark_running + attach pump
    Service-->>Browser: 202 + logical turn_id + Goal

    loop 自動 continuation Turns
        Codex-->>Goal: physical Turn notifications
        Goal-->>Service: logical turn_id notifications + goal updates
        Service->>Hub: publish SSE
        Hub-->>Browser: timeline/status/usage updates
    end

    Codex-->>Goal: complete/blocked/limited/paused
    Goal-->>Service: one logical completion
    Service->>Turns: finish
    Service->>Hub: console.goal.idle
```

Goal active 期間與一般 Turn、fork、archive 等 mutation 互斥。一般 composer 文字仍可 steer 目前的 physical Goal Turn；`Pause`／`Stop` 會先把 Goal 設為 paused 再 interrupt physical Turn，避免 continuation 自動重啟。Graceful shutdown 同樣透過 logical handle pause 並 drain Goal pump。Browser 中斷 SSE 不會停止 Goal，重連後沿用 EventHub replay；超出 replay window 時重新讀取 Thread history 與 Goal snapshot。

Codex Goal RPC 本身不帶 model 或 reasoning effort 欄位，因此 Web 在啟動或恢復 Goal 前，會在同一個 per-thread reservation 內先用 `thread/resume` 套用目前選定的 model 與 `config.model_reasoning_effort`。後續由 runtime 自動建立的 continuation Turns 便沿用該 Thread 設定；`console.goal.starting` 與 `console.goal.running` 也會攜帶相同 model／reasoning effort，讓其他分頁及重新渲染後的 Inspector 顯示一致。

## SSE reconnect、replay 與 resync

每個 Thread 都有 process-local 單調 sequence 與有限 history。SSE event 的 `id` 就是 sequence；Browser 每成功處理一筆 event，也會在頁面記憶體內保存該 Thread 的最後 sequence。原生 `EventSource` 因網路問題自動 reconnect 時會用 `Last-Event-ID` 要求缺少的事件；使用者主動切換 Session 時會關閉舊 SSE，切回後建立帶有 `after_sequence` query cursor 的新 SSE。這份 per-Thread cursor 不寫入 SQLite 或 Browser persistent storage，重新整理頁面後仍以 Codex Thread history 重新建立基準。完整設計、邊界案例與測試對照請見 [Session event replay](session-event-replay.md)。

```mermaid
flowchart TD
    connect["SSE connect / reconnect"] --> cursor["自動重連使用 Last-Event-ID<br/>Session 切回使用 after_sequence"]
    cursor --> auth["重新驗證 Thread 與 CWD"]
    auth --> subscribe["依有效 cursor 建立 bounded subscriber queue"]
    subscribe --> range{"cursor 是否存在且<br/>超出目前 replay window？"}
    range -->|否，沒有 ID| ready["送出 console.stream.ready"]
    range -->|否，仍可 replay| replay["依序 replay 較新的 events"]
    replay --> ready
    range -->|是：backend restart、future id 或 history gap| resync["送出 console.stream.resync_required"]
    resync --> refresh["Browser 重新取得 authoritative Thread partials"]
    refresh --> ready
    ready --> live["等待 live events<br/>閒置時每 15 秒 heartbeat"]
    live --> overflow{"subscriber queue overflow？"}
    overflow -->|否| live
    overflow -->|是| drop["移除 slow subscriber<br/>送出 resync_required 並關閉 stream"]
    drop --> refresh_closed["Browser 重新取得 authoritative Thread partials"]
    refresh_closed --> connect
```

Replay 只用來補足短暫斷線，不是持久化 event store。遇到 process restart、history gap 或 slow subscriber overflow 時，UI 必須回到 Codex Thread history 重新同步，不能把本地 live event list 當成權威資料。

`Last-Event-ID` header 與 `after_sequence` 同時存在時，Backend 以 header 為準，因為它代表同一個 `EventSource` 已經在目前 URL cursor 之後成功收到的新事件。若 cursor 早於 EventHub 最舊保留事件，或 Backend restart 後 cursor 大於重新開始的 sequence，Backend 會送出 `console.stream.resync_required`。Browser 此時以事件攜帶的目前 sequence 重設該 Thread cursor、清除 transient live state，並重新讀取 Codex Thread／Goal 的 authoritative partials；同一條 SSE 隨後繼續承接較新的 live events。

## 關閉中的 Turn

收到 shutdown 後，`CodexRuntime` 先將 `ready` 設為 false，`TurnManager` 停止接受新 Turn，接著對所有可控制的 handles 發出 interrupt。Event-pump tasks 會在 timeout 內 drain；逾時的 task 會被 cancel。完成後才關閉 `AsyncCodex`，最後由 process entrypoint 停止 scheduler 並 dispose database engine。
