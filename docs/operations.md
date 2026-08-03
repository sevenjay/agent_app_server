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
> `TrustedHostMiddleware` 只驗證 HTTP `Host` header，不提供使用者驗證。production 目前也不會自動切換成較保守的 Codex permissions。部署者必須在 `.secrets.toml` 覆寫合適的 bind、trusted hosts、approval mode 與 sandbox，並提供 authentication、TLS 或等價的受信任存取層。

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
```

Web Permissions 卡片會用 Codex CLI 對應名稱呈現權限：

| 設定值 | Codex CLI 對照 | 行為 |
| --- | --- | --- |
| `codex_sandbox = "read_only"` | `read-only` | 只允許讀取 |
| `codex_sandbox = "workspace_write"` | `workspace-write` | 可寫入 workspace／writable roots |
| `codex_sandbox = "full_access"` | `danger-full-access` | 移除 sandbox 限制 |
| `codex_approval_mode = "auto_review"` | `Approve for me` | `on-request` 並自動審查提升權限請求 |
| `codex_approval_mode = "deny_all"` | `never` | 拒絕所有提升權限請求 |

### Project Registry

`codex_projects_root` 必須指向存在且可讀的目錄，建議一律使用絕對路徑。server 在 startup 時會展開 `~`、`resolve()` root，並將每個第一層實體目錄註冊成 Project；symbolic link 不會被納入。

```toml
codex_projects_root = "/home/you/codex-workspaces"
codex_hidden_projects = ["private-tools", "internal notes"]
```

`codex_hidden_projects` 只會從 Web selector 隱藏指定的 project key 或目錄名稱，不會將 Project 移出 server registry，也不會繞過既有 Thread 的 CWD authorization。

Browser 只能提交 server 產生的 `project_key`。建立 Project 時只接受單一目錄名稱；Thread read、mutation、Turn 與 Goal 操作都會重新確認 Thread 實際 CWD 位於 registry 內。

Files API 只接受 project-relative path，拒絕 absolute path、`..`、backslash、control characters 與 symbolic link。上傳採用同目錄暫存檔後原子放置；刪除資料夾會遞迴移除其內容。

### Codex authentication

應用程式沿用執行服務之 Linux user 的 `~/.codex`。它不接受、保存或記錄 Browser 提交的 Codex API key。部署時應使用專用 service account，並審核該帳號的 Project、network 與其他 filesystem permissions。

## Database 與 migration

預設 SQLite 位於 repository 外的 `../agent_app_server_data/app.db`。連線會啟用 WAL、foreign keys、5 秒 busy timeout 與 pool pre-ping。

目前 schema：

- `thread_ui_metadata`：project key、pin、custom label、last-opened 與 timestamps。
- `app_settings`：最後選擇的 Project／Session 等少量 UI preferences。

SQLite 不保存 prompt、agent response、command output、diff、Goal、token usage 或 Codex conversation mirror。

Migration commands：

```bash
poetry run alembic upgrade head
poetry run alembic current
```

可用 `DATABASE_URL` 覆寫位置：

```bash
DATABASE_URL=sqlite+aiosqlite:////absolute/path/app.db poetry run python main.py
```

執行 `poetry run python -m scripts.backup_database` 會使用 SQLite online backup API，在 database 同層的 `backups/` 建立一致的 `<stem>-<UTC timestamp>.db`。來源 database 尚不存在或使用 in-memory database 時會安全略過。

## Frontend workflow

前端是 server-rendered HTML，不使用 React、Vue、TypeScript 或 application bundler：

- Jinja2 負責 HTML partials。
- HTMX 2.0.4 負責 partial request／swap。
- Alpine.js 3.14.9 管理 browser state 與操作協調。
- 原生 `EventSource` 接收目前 Session 的 SSE；Alpine.js 在頁面記憶體保存 per-Session replay cursor，切回時補收 EventHub 仍保留的事件。
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

Python tests 使用 fake Codex adapter，不啟動真實 app-server、不要求 login，也不修改真實 Project。測試涵蓋 Project／CWD authorization、Files path safety、SDK serialization、Turn race、跨 Session concurrency、Goal lifecycle、SSE replay／overflow／resync、API／partials、metadata concurrency、runtime cleanup、CDN／SRI 與 frontend contract。

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

完整服務必須維持單一 Uvicorn worker；多 worker 無法共享 active Turn、Goal handle、SSE history 與 subscriber state。理由請見[系統架構](architecture.md#為何限制單一-worker)。

## Logs 與隱私

- Application lifecycle：`logs/agent_app_server.log`
- Uvicorn access：`logs/uvicorn-access.log`
- Uvicorn errors：`logs/uvicorn-error.log`

Uvicorn files 以 5 MiB 輪替並保留 3 份。Access log 會遮蔽 query string 並跳脫 control characters。應用程式只記錄狀態、request correlation 與 exception type，不應記錄完整 prompt、diff、command output、token 或 cookie。
