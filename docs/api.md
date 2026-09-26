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
| `POST` | `/api/codex/rate-limit-reset-credits/consume` | 在 5h 或 Weekly limit 剩餘不超過 1% 時使用一張 reset credit |
| `GET` | `/api/codex/models` | 可用 models 與 reasoning efforts |
| `GET` | `/api/preferences` | 讀取 Web UI preferences |
| `PATCH` | `/api/preferences` | 更新最後選擇的 Project／Thread |

`POST /api/projects` 只接受單一目錄名稱。Browser 不會提交任意 absolute CWD。

## Project files

所有 path 都相對於指定 Project root；空字串代表 root。

| Method | Path | 用途 |
| --- | --- | --- |
| `GET` | `/api/projects/{project_key}/files?path=&show_hidden=false` | 列出一層目錄內容；預設略過 `.` 開頭項目 |
| `GET` | `/api/projects/{project_key}/files/download?path=` | 下載單一 regular file，或將目錄打包為 ZIP（略過 symbolic link／special file） |
| `GET` | `/api/projects/{project_key}/files/preview?path=&show_hidden=false` | 獨立 HTML 預覽頁；支援目錄、UTF-8 文字、圖片、PDF 與瀏覽器支援的影音格式 |
| `GET` | `/api/projects/{project_key}/files/preview/content?path=` | 以 inline 回傳支援的圖片／PDF／影音；HTML、SVG 與程式碼只在預覽頁以文字呈現 |
| `POST` | `/api/projects/{project_key}/files/directories` | 建立資料夾；JSON body 為 `path`、`name` |
| `POST` | `/api/projects/{project_key}/files/upload?path=&name=&overwrite=` | 以 raw request body 上傳一個檔案 |
| `PATCH` | `/api/projects/{project_key}/files` | 重新命名檔案或資料夾；JSON body 為 `path`、`name` |
| `DELETE` | `/api/projects/{project_key}/files?path=` | 刪除檔案，或遞迴刪除資料夾 |

File manager 拒絕 absolute path、path traversal、Windows-style separator、control characters、symbolic link 與 special file。預設不覆寫同名上傳；只有明確傳入 `overwrite=true` 才會取代既有 regular file。

`.stream_journal` 是保留區，即使 `show_hidden=true` 也不列出；Files API 拒絕存取、建立、覆寫、重新命名或刪除這棵目錄。

列表回傳 `git_available`，每個項目附 `git_status`（`modified`、`added`、`deleted`、`renamed`、`conflicted`、`untracked`、`ignored` 或 `null`）及單檔的 `git_status_code`（Git porcelain XY）。目錄彙整後代變更；已刪除檔案透過仍存在的父目錄呈現。未變更或無法讀取 Git 狀態時為 `null`，不顯示標記。支援 `.git` 目錄及 worktree 的 `.git` 檔案；Git 不可用時仍可正常瀏覽檔案。

Files 每列 hover／鍵盤 focus 時，依序顯示 Preview、Download、Rename、Delete、Info 圖示；觸控裝置持續顯示。Preview 在新分頁開啟；文字最多預覽 1 MiB，二進位或不支援的格式提供下載提示。Info 顯示路徑、類型、大小、修改時間與 Git 狀態。

## 對話附件

| Method | Path | 用途 |
| --- | --- | --- |
| `POST` | `/api/projects/{project_key}/conversation-attachments?name=...` | 一個 raw-body 檔案，回 `201 {id, name, mime, size}`；逐塊接收並限制實際位元組 |
| `DELETE` | `/api/projects/{project_key}/conversation-attachments/{id}` | 刪除同 Project 尚未提交的附件，成功回 `204` |
| `GET` | `/api/codex/threads/{thread_id}/attachments/{id}` | 經 Thread／Project 授權後預覽 PNG/JPEG 或下載文字檔 |

支援 PNG、JPEG，以及 UTF-8 `.txt`、`.md`、`.log`；每則最多 5 檔、每檔 50 MiB、合計 250 MiB。圖片簽章與文字編碼由伺服器驗證，瀏覽器 MIME 不作為判斷依據。同名檔案有不同 ID，不覆寫；拒絕 symlink、special file、任意檔案路徑、重複 ID、跨 Project 或已提交 ID。

`POST /api/codex/threads` 可帶 `initial_attachment_ids: string[]`，必須搭配非空的 `initial_prompt`。既有 Thread 的 `POST .../turns` 可帶 `attachment_ids: string[]`，必須搭配 `prompt`。兩者預設 `[]`，保留舊的純文字請求。Steer 與 Goal schema 不接受附件欄位；含附件的 `/goal` prompt 也會被拒絕。活動中的 Turn／Goal 不接受附件新訊息。

圖片由 `LocalImageInput` 傳送，文字檔則透過伺服器產生的名稱／可讀路徑清單讓 agent 按需讀取。Timeline／SSE 的使用者訊息只呈現使用者原文與 `attachments` 安全 metadata（`id`、`name`、`mime`、`size`、`delivery`）；不提供由客戶端指定的路徑。Timeline 附件另有 `available` 狀態，已遺失的檔案不提供下載卡片。

未提交附件於啟動、runtime health 定期巡查或後續上傳時清理，期限 24 小時。已提交附件跟隨 Stream Journal 保留與 Thread 刪除流程（預設 30 天）；過期後不保證可下載。Fork 不複製原 Thread 附件，缺少 metadata 的 history fallback 顯示附件不可用。

`400 invalid_attachment` 表示格式、容量或檔案檢查失敗；`404 attachment_unavailable` 表示無法找到符合 scope／狀態的附件。若 SDK 可能已接受含附件的 Turn，但結果不明，回 `503 attachment_submission_uncertain` 並保留 Thread／附件；請先查看 Session history，不要自動重送。

## Threads（Sessions）

| Method | Path | 用途 |
| --- | --- | --- |
| `GET` | `/api/codex/threads` | 依 `project_key`、`archived`、`cursor`、`limit` 列出 Threads |
| `POST` | `/api/codex/threads` | 在 Project 建立 Thread；可用 `initial_prompt` 啟動普通 Turn，或用 `initial_goal` 啟動 logical Goal |
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
新 Session 的 composer 若以 `/goal <objective>` 開始，會使用 `initial_goal` 直接建立 logical Goal，不會先把 `/goal` 當成普通首個 Turn。

## Turns 與 events

| Method | Path | 用途 |
| --- | --- | --- |
| `POST` | `/api/codex/threads/{thread_id}/turns` | 啟動新 Turn；若 Thread 有未受本 process 管理的 active persisted Goal，回 `409` |
| `POST` | `/api/codex/threads/{thread_id}/steer` | 對活動 Turn／Goal 追加指示 |
| `POST` | `/api/codex/threads/{thread_id}/interrupt` | 中止活動 Turn |
| `GET` | `/api/codex/threads/{thread_id}/events` | 訂閱該 Thread 的 SSE stream；可帶 `after_sequence` replay cursor |

SSE 支援 `Last-Event-ID`、`after_sequence` query cursor、JSONL durable replay、comment heartbeat 與 `console.stream.resync_required`。同一個原生 `EventSource` 自動重連時，`Last-Event-ID` 優先於 URL query；主動切換 Session 則使用 snapshot 的 `journal_cursor`。Backend 先註冊 live subscriber，再 replay Journal `seq > cursor`，最後以 high-water 過濾 queue 重複。Browser 收到 `console.stream.ready` 後才啟用送出操作。詳細行為請見[操作流程](flows.md#sse-reconnectreplay-與-resync)。

## HTML partials

| Method | Path | 用途 |
| --- | --- | --- |
| `GET` | `/partials/codex/status` | Runtime、account、usage 與 model 狀態；`refresh_limits=true` 會立即重讀 account limits |
| `GET` | `/partials/projects` | Project selector |
| `GET` | `/partials/threads` | Session list，支援 archived 與 cursor |
| `GET` | `/partials/threads/{thread_id}/timeline` | Journal Timeline snapshot，根元素帶 durable cursor／coverage |
| `GET` | `/partials/threads/{thread_id}/inspector` | Session、Goal、plan 與 usage details |
| `GET` | `/partials/threads/{thread_id}/changes` | Live changes／diff view |
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

`/api/projects`、`/api/codex`、`/api/preferences` 與 `/partials` routes 都接上 `require_web_user` dependency；`/`、`/static`、`/api/status` 與 OpenAPI endpoints 不經過它。

`single_user` 模式直接建立固定 local identity。`multi_tenant` 模式要求 oauth2-proxy 的 subject 與 preferred-username headers，並將 Project、Thread、SSE、metadata 與 preferences 限制在該 tenant。詳細 `/api/status` 刻意維持公開，可能揭露共用 Codex account 與 runtime 資訊。
