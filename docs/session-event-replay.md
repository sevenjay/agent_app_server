# Stream Journal、Timeline snapshot 與 SSE replay

Web console 不再把 Codex `thread.read(include_turns=True)` 當成完整執行軌跡的唯一來源。Backend 觀察到的 scoped／global notifications 會先正規化、遮蔽並寫入 Project 擁有的 per-thread JSONL，成功後才 fan out 至 SSE。

## 權責

| 來源 | 用途 | 是否持久 |
| --- | --- | --- |
| `.stream_journal/<thread_id>/events.jsonl` | Web console 觀察到的 conversation、command／tool activity、順序與 replay cursor | 是 |
| Codex Thread history | absent／partial Journal 的 conversation fallback、Turn 最終狀態 | Codex 管理 |
| EventHub | 已持久事件的即時 fan-out、短期相容 cache、bounded subscriber queue | 否 |
| Browser state | 當前頁面的 live transient items 與最近 cursor | 否 |

Codex history 可能沒有 command output，而且 live `msg_...` 與 history `item-N` 可能代表同一訊息卻使用不同 ID。因此 Timeline 由 Journal materialize；history 僅 backfill Journal 缺少或 stream lifecycle 不完整的 Turn。Watermark 更新時，已有完整 live start／terminal 的 Turn 不會再次匯入。

## JSONL 與 durability

每個 Project 使用下列結構：

```text
project/
└── .stream_journal/              # 0700
    ├── <validated-thread-id>/     # 0700
    │   └── events.jsonl           # 0600
    └── .trash/
```

每行包含 schema version、per-thread `seq`、`event_id`、`dedup_key`、normalized `type`、原 SSE method、source IDs、timestamp 與 allowlisted data。所有 append 經同一個 writer queue；順序固定為：

```text
Codex notification
→ normalize / redact / truncate
→ append JSONL
→ terminal event fsync
→ EventHub publish（SSE id = Journal seq）
```

`agent_message.completed`、`command.completed` 與 Turn terminal events 會執行 `fsync`。Command／tool output 上限 64 KiB，保存原始 byte count 與 truncation flag；token、API key、cookie、password 與 Authorization value 會在落盤前遮蔽。Reasoning 與 hook prompt 不寫入 Journal。

## Coverage 與 history fallback

Coverage 逐 Turn 推導：

- `complete`：Journal stream 有 `turn.started`、terminal event，且檔案無已知 sequence／JSON 損壞。
- `partial`：缺 start／terminal、存在損壞或 stream gap。
- `absent`：尚無可 materialize 的 Journal／baseline。

第一次讀取舊 Session 時，Backend 將 history 以 deterministic dedup key lazy import，並追加 `history.baseline_imported` watermark。watermark 包含 updated time、Turn count 與 content fingerprint；同一 baseline 可重複請求而不重複寫入。後續 watermark 變更會逐 Turn reconciliation：完整 live Turn 只更新 watermark，缺少或 partial Turn 才匯入 history。

Partial merge 保留 Journal command／tool activity。Message／plan 只有在同一 turn、type 下存在唯一且完全相同的 normalized content 時才建立 alias；command／tool 還必須有唯一且相同的 normalized payload fingerprint。無法高信心配對的項目會同時保留並標記 `unresolved`。

Timeline item 使用 `console_item_id`，並帶有來源 alias：

```json
{
  "console_item_id": "timeline-...",
  "source_ids": {
    "codex_stream": "msg_live_id",
    "codex_history": "item-2"
  }
}
```

新 alias 以 `item.alias_attached` 追加，不原地修改 JSONL。這使 history ID 與 live ID 不同時仍只 materialize 一則回答。

## Snapshot → replay → live

Browser 選取 Session 時先載入 `GET /api/codex/threads/{thread_id}/snapshot` 或相同 materializer 產生的 timeline partial。回應包含：

```json
{
  "journal_cursor": 248,
  "journal_coverage": "complete"
}
```

Timeline partial 將值放在 `data-journal-cursor` 與 `data-journal-coverage`。前端記住 cursor，清除所有 `sequence <= cursor` 的 transient items，再建立：

```text
GET /api/codex/threads/{thread_id}/events?after_sequence=248
```

同一個 materialized view 也回傳最新的 Journal diff 與 usage snapshot；Changes／Inspector partial 會以各自的 cursor 收斂，且不會覆蓋在該 snapshot 之後才抵達的 live 更新。

後端會：

1. 重新驗證 Thread ID 與 Project CWD。
2. 先註冊 EventHub subscriber。
3. 從 Journal 讀取所有 `seq > 248`。
4. 送出 durable replay。
5. 送出 `console.stream.ready` 與 high-water cursor。
6. 切換到 subscriber queue，略過已包含在 replay high-water 內的重複 event。

步驟 2 早於 Journal read，因此 snapshot 建立後、SSE 訂閱前發生的 event 不會落入空窗。

同一個原生 `EventSource` 自動重連時，`Last-Event-ID` 優先於 URL 的 `after_sequence`。主動切換 Session 使用 snapshot cursor。Browser refresh 不需要持久保存舊 cursor，因為新的 snapshot 直接從 JSONL 建立最新基準。

## Resync 邊界

下列情況回傳 `console.stream.resync_required`：

- cursor 大於 Journal high-water sequence。
- Journal 有不完整尾端、中間 JSON 損壞或 sequence gap。
- slow subscriber queue overflow。

Browser 收到後會以 event 中的 durable sequence 取代 cursor、清除 transient state，並重讀 snapshot。Journal 不完整時 materializer 同時使用 Codex history conversation fallback；Journal 獨有 activity 不會被 history 覆蓋。

Backend restart 不會重設 sequence namespace。新的 EventHub 會對齊 JSONL high-water，後續 event 接續既有 sequence；完成的 user、agent、command 與 tool Timeline 可在沒有舊 Browser memory 的情況下重建。

## Duplicate notification

Turn／item terminal notification 使用 thread、turn、item 與 normalized type 的 deterministic key，可消除 Codex replay 與 scoped／global 重複。

Delta、plan、usage 與 status 可能合法地連續出現相同內容，不能永久以 payload fingerprint 去重。Service 只在短時間內、且同一 fingerprint 分別來自 scoped 與 global channel 時視為 duplicate；同一 channel 重複仍會配置不同 durable sequence。

## Crash、刪除與 retention

- 不完整最後一行：reader 忽略並標記 partial；下一次 append 會先移除 incomplete tail。
- 中間損壞：記錄不含 payload 的 warning、標記 partial，不靜默宣稱 complete。
- Thread delete：Codex 刪除成功後，把 Journal 移到 Project 內 `.stream_journal/.trash/`，可由管理者復原。
- Retention：runtime 啟動時依 `codex_journal_retention_days` 清理逾期 Journal／trash，預設 30 天。
- Logs：不得輸出 prompt、Journal event、command output 或 secret。

## 驗證對照

| 行為 | 主要測試 |
| --- | --- |
| sequence、dedup、權限與 thread ID path safety | `tests/test_stream_journal.py` |
| incomplete tail／中間損壞降級與後續 append recovery | `tests/test_stream_journal.py` |
| output redaction、64 KiB、truncation metadata | `tests/test_stream_journal.py` |
| live/history 不同 ID alias merge、command 保留 | `tests/test_stream_journal.py`、`tests/test_stream_journal_integration.py` |
| 沒有 subscriber 仍持久保存、restart snapshot／replay、snapshot/live 競態 | `tests/test_stream_journal_integration.py` |
| SSE header/query cursor、ready、resync、cleanup | `tests/test_app.py` |
| slow subscriber 與 EventHub fan-out | `tests/test_event_hub.py` |
