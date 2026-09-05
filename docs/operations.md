# Agent App Server 安裝與維運

本文件收錄本機開發、Dynaconf 設定、資料庫、前端、測試、logs 與 production 部署細節。系統元件與 process lifecycle 請見[系統架構](architecture.md)，HTTP contract 請見 [API 參考](api.md)。

## 執行需求

- Linux
- Python `>=3.12.3,<3.13`
- Poetry `>=2.0`
- Node.js 與 npm
- 執行服務的 Linux user 已完成 Codex 登入，且可存取自己的 `~/.codex`

Python dependencies 由 `poetry.lock` 鎖定，Tailwind build dependencies 由 `package-lock.json` 鎖定。

## 本機安裝與啟動

```bash
poetry install --with dev
npm ci
npm run tw:build
poetry run alembic upgrade head
poetry run python main.py
```

也可用 `scripts/run.sh` 啟動。該腳本會尋找 Poetry、設定 `PYTHONUNBUFFERED=1`，再執行 `poetry run python main.py`；不會 fetch code、安裝 dependencies、備份 database 或執行 migration。

development 預設位址：

- Web console：<http://127.0.0.1:8080>
- OpenAPI：<http://127.0.0.1:8080/docs>

若只想在不啟動 Codex subprocess 的情況下檢查 Web/runtime shell：

```bash
DYNACONF_CODEX_ENABLED=false poetry run python main.py
```

Project root 仍須是有效目錄。完整服務不可改用 `uvicorn main:app` 啟動，因為這會略過 `main.py` 管理的 database、scheduler 與 logging lifecycle。

## Dynaconf 設定

版本庫內的非秘密設定位於 `settings.toml`。本機或 host-specific override 應寫入已被 `.gitignore` 排除的 `.secrets.toml`：

```bash
cp .secrets.toml.example .secrets.toml
```

Dynaconf 預設選擇 `development`。production 必須明確設定：

```bash
export ENV_FOR_DYNACONF=production
```

目前版本庫內的 environment 設定如下：

| Environment | Bind | Trusted hosts | Approval / sandbox |
| --- | --- | --- | --- |
| `development` | `127.0.0.1:8080` | `localhost`、`127.0.0.1`、`testserver` | `auto_review` / `workspace_write` |
| `production` | `0.0.0.0:8080` | `localhost`、`127.0.0.1`、`192.168.50.234` | 繼承 `auto_review` / `workspace_write` |

> [!WARNING]
> `TrustedHostMiddleware` 只驗證 HTTP `Host` header。`multi_tenant` 只信任 oauth2-proxy headers，因此 backend 必須限制為 proxy 可連；`single_user` 則必須保持在受信任網路。production 不會自動切換成較保守的 Codex permissions。

建議以 host-specific 值覆寫，例如：

```toml
[development]
codex_projects_root = "/home/you/codex-workspaces"

[production]
codex_projects_root = "/srv/codex-workspaces"
web_host = "127.0.0.1"
trusted_hosts = ["console.example.internal"]
codex_approval_mode = "deny_all"
codex_sandbox = "read_only"
```

### Codex runtime 設定

```toml
codex_enabled = true
codex_approval_mode = "auto_review"
codex_sandbox = "workspace_write"
codex_event_history_limit = 2000
codex_subscriber_queue_limit = 1000
codex_shutdown_timeout_seconds = 10
codex_operation_timeout_seconds = 30
codex_sse_heartbeat_seconds = 15
codex_thread_lookup_page_limit = 50
codex_journal_retention_days = 30
```

### Deployment mode

```toml
deployment_mode = "single_user" # 或 "multi_tenant"
oauth2_proxy_subject_header = "X-Forwarded-User"
oauth2_proxy_username_header = "X-Forwarded-Preferred-Username"
```

`single_user` 維持 `<codex_projects_root>/<project>` 且不要求 headers。`multi_tenant` 使用 `<codex_projects_root>/<username>/<project>`，並以 oauth2-proxy identity 建立 tenant scope。完整部署方式見 [Keycloak、oauth2-proxy 與多租戶部署](multi-tenant.md)。

Web Permissions 卡片會用 Codex CLI 對應名稱呈現權限：

| 設定值 | Codex CLI 對照 | 行為 |
| --- | --- | --- |
| `codex_sandbox = "read_only"` | `read-only` | 只允許讀取 |
| `codex_sandbox = "workspace_write"` | `workspace-write` | 可寫入 workspace／writable roots |
| `codex_sandbox = "full_access"` | `danger-full-access` | 移除 sandbox 限制 |
| `codex_approval_mode = "auto_review"` | `Approve for me` | `on-request` 並自動審查提升權限請求 |
| `codex_approval_mode = "deny_all"` | `never` | 拒絕所有提升權限請求 |

### Project Registry

`codex_projects_root` 必須指向存在且可讀的目錄，建議一律使用絕對路徑。單用戶模式會將 root 的第一層實體目錄註冊成 Project；多租戶模式則先選擇 username 目錄，再將其中的第一層實體目錄註冊成 Project。symbolic link 不會被納入。

```toml
codex_projects_root = "/home/you/codex-workspaces"
codex_hidden_projects = ["private-tools", "internal notes"]
```

`codex_hidden_projects` 只會從 Web selector 隱藏指定的 project key 或目錄名稱，不會將 Project 移出 server registry，也不會繞過既有 Thread 的 CWD authorization。

Browser 只能提交 server 產生的 `project_key`。建立 Project 時只接受單一目錄名稱；Thread read、mutation、Turn 與 Goal 操作都會重新確認 Thread 實際 CWD 位於 registry 內。

Files API 只接受 project-relative path，拒絕 absolute path、`..`、backslash、control characters 與 symbolic link。Files 分頁預設隱藏 `.` 開頭的項目，可由 toolbar 的 **Show hidden** 選項顯示。上傳採用同目錄暫存檔後原子放置；刪除資料夾會遞迴移除其內容。

### Codex authentication

應用程式沿用執行服務之 Linux user 的 `~/.codex`。它不接受、保存或記錄 Browser 提交的 Codex API key。部署時應使用專用 service account，並審核該帳號的 Project、network 與其他 filesystem permissions。

### Codex CLI 版本與 Session 相容性

`codex_bin` 預設為空字串，使用 Python SDK 內附的 CLI；更新 shell 中的 `codex` 不會更新這個內附版本。若 Session 由較新的 CLI 建立，舊 runtime 可能回覆 `paginated_threads is not supported yet`，造成選取 Session、preferences PATCH 或面板載入失敗。

可在 `.secrets.toml` 指定已安裝的新版執行檔，並重啟服務：

```toml
[production]
codex_bin = "/home/jack/.npm-global/bin/codex"
```

也可設定 `DYNACONF_CODEX_BIN`。systemd 的 PATH 可能不同於互動 shell，建議使用 `which codex` 顯示的絕對路徑；無效路徑會在啟動時明確報錯。啟動日誌會記錄使用內附或指定的執行檔。外部 CLI 的升級由管理者處理，需確認與目前 Python SDK 的 RPC 相容。

選取 Session、載入面板與讀取 Goal 使用 `thread/read`，不先 resume 或取得 session 寫入鎖；即使另一個 Codex 程序正在使用該 Session，也可讀取已保存的內容。開始 Turn／Goal 等寫入操作仍須 resume。

## Database 與 migration

預設 SQLite 位於 repository 外的 `../agent_app_server_data/app.db`。連線會啟用 WAL、foreign keys、5 秒 busy timeout 與 pool pre-ping。

目前 schema：

- `tenants`：external identity、username 與固定 workspace directory 的映射；不保存 credential 或 token。
- `thread_ui_metadata`：以 tenant、thread 為 scope 的 project key、pin、custom label、last-opened 與 timestamps。
- `app_settings`：以 tenant 為 scope 的 Project／Session 等少量 UI preferences。

SQLite 不保存 prompt、agent response、command output、diff、Goal、token usage 或 Codex conversation mirror。

升級到 tenant schema 時，既有 metadata 會歸入 `local-service-user`。從 tenant schema downgrade 只保留該 local owner 的 metadata，會捨棄其他 tenant mappings 與 UI metadata；執行 downgrade 前必須先備份 database。

舊版啟動時使用 `create_all()` 建表，可能沒有 Alembic revision，或已建立 `tenants` 但 metadata 仍是舊 schema。`alembic upgrade head` 會驗證並接管這些已知 schema，再完成升級；不需要刪除 database 或手動 `stamp head`。若表結構不完整或不符合已知版本，migration 會停止，保留資料供檢查。啟動時若偵測到尚未升級的單用戶 schema，也會停止並提示 migration 指令，避免 API 在啟動後才回覆 HTTP 500。

`single_user` 同樣需要 tenant schema，但仍使用 `local-service-user`，不要求登入或 proxy headers，也不改變既有 Project／Session 路徑。

## Stream Journal

每個有 Session 的 Project 會建立 `.stream_journal/<thread_id>/events.jsonl`。目錄與檔案權限分別固定為 `0700` 與 `0600`；Thread ID 在組合 path 前必須通過格式驗證。Journal 保存經 allowlist／redaction 的 user、agent、command、tool、file、web、plan 與 usage events，SSE sequence 直接沿用 JSONL `seq`。

`codex_journal_retention_days` 預設 30。Runtime 啟動時會清除最後寫入時間早於 retention 的 Thread Journal，以及逾期 `.trash` 項目。刪除 Session 時不立即抹除 JSONL，而是先移至 Project 的 `.stream_journal/.trash/`，以便管理者在 retention 到期前復原。

SQLite online backup 不包含 `.stream_journal/`。若部署需要備份完整 Timeline，必須另外備份各 Project 的 `.stream_journal/`，並採用不低於 database backup 的 owner、mode、加密與存取控制；不要把 Journal 放進公開 artifact 或一般 application log。

Migration commands：

```bash
poetry run python -m scripts.backup_database
poetry run alembic upgrade head
poetry run alembic current
```

既有服務升級時先停止服務，完成上述備份與 migration 後再啟動。

可用 `DATABASE_URL` 覆寫位置：

```bash
DATABASE_URL=sqlite+aiosqlite:////absolute/path/app.db poetry run python main.py
```

執行 `poetry run python -m scripts.backup_database` 會使用 SQLite online backup API，在 database 同層的 `backups/` 建立一致的 `<stem>-<UTC timestamp>.db`。來源 database 尚不存在或使用 in-memory database 時會安全略過。

## Frontend workflow

前端是 server-rendered HTML，不使用 React、Vue、TypeScript 或 application bundler：

- Jinja2 負責 HTML partials。
- HTMX 2.0.4 負責 partial request／swap。
- Alpine.js 3.16.3 管理 browser state 與操作協調。
- HTMX timeline snapshot 提供 durable Journal cursor；Alpine.js 清除已由 snapshot 涵蓋的 transient items，再由原生 `EventSource` 從 JSONL cursor replay 並接上 EventHub live fan-out。
- Marked 15.0.12 解析 Markdown，再由 DOMPurify 3.2.6 sanitize。
- Tailwind CSS 4 是唯一需要建置的 frontend asset。

CDN scripts 固定版本並帶 SRI。Tailwind commands：

```bash
npm run tw:dev
npm run tw:build
```

輸入來源包含 `static/index.html`、`templates/**/*.html` 與 `static/js/**/*.js`，輸出為 `static/css/tailwind.css`。

## 測試與靜態檢查

```bash
poetry install --with dev
poetry run python -m pytest -q
pipx run ruff check .
npm ci
npm run tw:build
```

Python tests 使用 fake Codex adapter，不啟動真實 app-server、不要求 login。Stream Journal 專用測試使用 pytest temporary Projects；測試涵蓋 durable replay、crash tail、redaction／truncation、cross-source alias、retention，以及既有 Project／CWD authorization、Turn、Goal、API／partials、runtime 與 frontend contracts。

## 部署

`.github/workflows/deploy.yml` 會在 PR merge 到 `master` 或手動觸發時，於帶有 `self-hosted`、`linux`、`x64`、`agent-app-server` labels 的 runner 執行：

1. `systemctl stop agent-app-server`
2. `/srv/agent-app-server/scripts/deploy.sh`
3. `systemctl start agent-app-server`
4. `systemctl status agent-app-server --no-pager`

`scripts/deploy.sh` 本身不啟動服務。預設行為為：

1. 確認 tracked worktree 無本機修改。
2. fetch `origin/master`，並將 worktree reset 到該 ref。
3. 執行 `poetry install --only main --no-root --no-interaction`。
4. 建立 migration 前的 SQLite backup。
5. 執行 `alembic upgrade head` 與 `alembic current`。

可用 `scripts/deploy.sh --help` 查看 `DEPLOY_REMOTE`、`DEPLOY_BRANCH`、`DEPLOY_APP_DIR` 與 skip flags。`DEPLOY_ALLOW_DIRTY=1` 只會略過 tracked worktree 保護，後續 hard reset 仍會覆寫修改，使用前必須確認資料可丟棄。

完整服務必須維持單一 Uvicorn worker；Journal replay 雖已持久化，多 worker 仍無法共享 active Turn、Goal handle、writer ordering 與 subscriber state。理由請見[系統架構](architecture.md#為何限制單一-worker)。

## Logs 與隱私

- Application lifecycle：`logs/agent_app_server.log`
- Uvicorn access：`logs/uvicorn-access.log`
- Uvicorn errors：`logs/uvicorn-error.log`

Uvicorn files 以 5 MiB 輪替並保留 3 份。Access log 會遮蔽 query string 並跳脫 control characters。應用程式只記錄狀態、request correlation 與 exception type，不應記錄完整 prompt、diff、command output、token 或 cookie。
