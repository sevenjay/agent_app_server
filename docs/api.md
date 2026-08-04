# Agent App Server API 參考

本文件列出目前 Web console 使用的 JSON API、HTML partials 與 SSE endpoint。互動式 OpenAPI 在服務啟動後可由 `/docs` 查看；主要操作 sequence 請見[操作流程](flows.md)。

Web UI 稱一段對話為 Session；API 與 Codex SDK 使用 Thread／`thread`。

## Runtime、Projects 與 preferences

| Method | Path | 用途 |
| --- | --- | --- |
| `GET` | `/api/status` | Database、scheduler 與 Codex runtime health |
| `GET` | `/api/projects` | 列出可見 Projects |
| `POST` | `/api/projects` | 在設定的 root 建立一個 Project 目錄 |
| `GET` | `/api/codex/account` | Codex account 與 usage limits |
| `GET` | `/api/codex/models` | 可用 models 與 reasoning efforts |
| `GET` | `/api/preferences` | 讀取 Web UI preferences |
| `PATCH` | `/api/preferences` | 更新最後選擇的 Project／Thread |

`POST /api/projects` 只接受單一目錄名稱。Browser 不會提交任意 absolute CWD。

## Project files

所有 path 都相對於指定 Project root；空字串代表 root。

| Method | Path | 用途 |
| --- | --- | --- |
| `GET` | `/api/projects/{project_key}/files?path=` | 列出一層目錄內容 |
| `GET` | `/api/projects/{project_key}/files/download?path=` | 下載單一 regular file |
| `POST` | `/api/projects/{project_key}/files/directories` | 建立資料夾；JSON body 為 `path`、`name` |
| `POST` | `/api/projects/{project_key}/files/upload?path=&name=&overwrite=` | 以 raw request body 上傳一個檔案 |
| `PATCH` | `/api/projects/{project_key}/files` | 重新命名檔案或資料夾；JSON body 為 `path`、`name` |
| `DELETE` | `/api/projects/{project_key}/files?path=` | 刪除檔案，或遞迴刪除資料夾 |

File manager 拒絕 absolute path、path traversal、Windows-style separator、control characters、symbolic link 與 special file。預設不覆寫同名上傳；只有明確傳入 `overwrite=true` 才會取代既有 regular file。

## Threads（Sessions）

| Method | Path | 用途 |
| --- | --- | --- |
| `GET` | `/api/codex/threads` | 依 `project_key`、`archived`、`cursor`、`limit` 列出 Threads |
| `POST` | `/api/codex/threads` | 在 Project 建立 Thread |
| `GET` | `/api/codex/threads/{thread_id}` | 讀取 Journal materialized Thread／Timeline；partial 時合併 Codex history |
| `GET` | `/api/codex/threads/{thread_id}/snapshot` | 讀取含 cursor、coverage、最新 diff／usage 的 materialized snapshot |
| `PATCH` | `/api/codex/threads/{thread_id}` | 更新名稱、pin 或 custom label |
| `DELETE` | `/api/codex/threads/{thread_id}` | 刪除 Thread 與 UI metadata，Journal 先移至 retention 管理的 trash |
| `POST` | `/api/codex/threads/{thread_id}/fork` | Fork Thread |
| `POST` | `/api/codex/threads/{thread_id}/archive` | 封存 Thread |
| `POST` | `/api/codex/threads/{thread_id}/unarchive` | 解除封存 Thread |

每次 Thread read、mutation、Turn 或 Goal 操作都會重新驗證 Thread 的實際 CWD 是否屬於 Project Registry；不在 allow-list 時回 `404`。

## Goals

| Method | Path | 用途 |
| --- | --- | --- |
| `GET` | `/api/codex/threads/{thread_id}/goal` | 讀取目前 Goal snapshot |
| `POST` | `/api/codex/threads/{thread_id}/goal` | 啟動 Goal，可帶 objective、token budget、model 與 reasoning effort |
| `PATCH` | `/api/codex/threads/{thread_id}/goal` | 將 Goal 切換為 `active` 或 `paused` |
| `DELETE` | `/api/codex/threads/{thread_id}/goal` | 清除 Goal |

Goal 狀態與 usage 由 Codex Thread store 保存，不鏡像到 SQLite。Goal 的多個 physical continuation Turns 在 Web runtime 中會合併成一個 logical operation。

## Turns 與 events

| Method | Path | 用途 |
| --- | --- | --- |
| `POST` | `/api/codex/threads/{thread_id}/turns` | 啟動新 Turn |
| `POST` | `/api/codex/threads/{thread_id}/steer` | 對活動 Turn／Goal 追加指示 |
| `POST` | `/api/codex/threads/{thread_id}/interrupt` | 中止活動 Turn |
| `GET` | `/api/codex/threads/{thread_id}/events` | 訂閱該 Thread 的 SSE stream；可帶 `after_sequence` replay cursor |

SSE 支援 `Last-Event-ID`、`after_sequence` query cursor、JSONL durable replay、comment heartbeat 與 `console.stream.resync_required`。同一個原生 `EventSource` 自動重連時，`Last-Event-ID` 優先於 URL query；主動切換 Session 則使用 snapshot 的 `journal_cursor`。Backend 先註冊 live subscriber，再 replay Journal `seq > cursor`，最後以 high-water 過濾 queue 重複。Browser 收到 `console.stream.ready` 後才啟用送出操作。詳細行為請見[操作流程](flows.md#sse-reconnectreplay-與-resync)。

## HTML partials

| Method | Path | 用途 |
| --- | --- | --- |
| `GET` | `/partials/codex/status` | Runtime、account、usage 與 model 狀態 |
| `GET` | `/partials/projects` | Project selector |
| `GET` | `/partials/threads` | Session list，支援 archived 與 cursor |
| `GET` | `/partials/threads/{thread_id}/timeline` | Journal Timeline snapshot，根元素帶 durable cursor／coverage |
| `GET` | `/partials/threads/{thread_id}/inspector` | Session、Goal、plan 與 usage details |
| `GET` | `/partials/threads/{thread_id}/changes` | Latest changes／diff view |
| `GET` | `/partials/threads/{thread_id}/composer` | Prompt composer |

## 錯誤格式

預期錯誤使用穩定 code 與可安全顯示的訊息：

```json
{
  "error": {
    "code": "file_not_found",
    "message": "The requested file or folder was not found. Refresh the file list and try again."
  }
}
```

常見 HTTP status：

| Status | 類型 |
| --- | --- |
| `400` / `422` | 無效 request、path 或 schema validation |
| `403` | Project file permission denied |
| `404` | Project／Thread／file 不存在，或 CWD 不在 allow-list |
| `409` | 同名項目、active-state 或 mutation conflict |
| `503` | Project root 或 Codex runtime unavailable |
| `504` | Codex operation timeout |

## Authentication 狀態

`/api/projects`、`/api/codex`、`/api/preferences` 與 `/partials` routes 都接上 `require_web_user` dependency；`/`、`/static`、`/api/status` 與 OpenAPI endpoints 則不經過它。該 dependency 目前仍不執行真正的 authentication，因此服務只能放在 loopback，或已有可靠 authentication、TLS 與 network access control 的受信任環境中。
