# Agent App Server

Agent App Server 是部署在固定 Linux 工作站上的單一使用者 Codex Web 控制台。使用者可以從桌面或手機瀏覽器管理 Project、Session 與工作區檔案，啟動 Codex Turn 或長時間 Goal，並即時查看訊息、工具輸出、plan、usage 與程式碼變更。

Web console 觀察到的對話與執行軌跡會先寫入各 Project 的 per-thread Stream Journal；Codex history 在 Journal absent／partial 時提供 conversation fallback。SQLite 只保存 pin、label、最近開啟時間與最後選擇等 Web UI metadata。

> [!WARNING]
> 目前的 `require_web_user` 只是預留的整合接點，不是正式的使用者驗證。development 綁定 `127.0.0.1:8080`，但目前版本庫內的 production 設定會綁定 `0.0.0.0:8080`。在補上 authentication、TLS 與受信任的存取層，或改回 loopback 以前，請勿將服務直接公開到 Internet。

## Screenshot
<p align="center">
  <img width="95%" height="992" alt="image" src="https://github.com/user-attachments/assets/7e8daea0-36f2-4675-8fd5-0f2939d14890" />
  <img width="30%" height="2712" alt="Screenshot_2026-08-18-11-28-10-486_org mozilla firefox" src="https://github.com/user-attachments/assets/78c60385-119e-45f3-a1e1-ccf1339342d5" />
  <img width="30%" height="2712" alt="Screenshot_2026-08-18-11-29-54-017_org mozilla firefox" src="https://github.com/user-attachments/assets/2812f048-ec5d-4bd7-a88f-54c8603bc1a9" />
  <img width="30%" height="2712" alt="Screenshot_2026-08-18-11-30-58-386_org mozilla firefox" src="https://github.com/user-attachments/assets/0a7404cb-6857-4c51-a66d-c239beda6b4f" />
  <img width="30%" height="2712" alt="Screenshot_2026-08-18-11-32-08-267_org mozilla firefox" src="https://github.com/user-attachments/assets/4a9349f8-5faf-4d37-b613-4251ad824de2" />
  <img width="30%" height="2712" alt="Screenshot_2026-08-18-11-42-17-683_org mozilla firefox" src="https://github.com/user-attachments/assets/4f998e58-713b-41fd-971d-803f57b94006" />
  <img width="30%" height="2712" alt="Screenshot_2026-08-18-11-42-42-349_org mozilla firefox" src="https://github.com/user-attachments/assets/622b69aa-c32f-46be-9ca0-5fd918eed0c9" />
</p>




## 目前功能

- **Projects**：從固定的 server-side root 探索或建立工作目錄，Browser 不需也不能提交任意 CWD。
- **Sessions**：建立、重新命名、pin、fork、封存、解除封存與刪除既有 Codex Threads。
- **即時執行**：啟動 Turn，透過 SSE 串流 agent 訊息、tool results、plan、diff 與 usage；活動中的 Turn 可 steer 或 interrupt。
- **Long-running Goals**：可從 Inspector 或 composer `/goal` 指令啟動、查看、暫停、恢復與清除 Goal。
- **檔案管理**：在 Files 分頁瀏覽 Project tree、上傳／下載檔案、新增資料夾、重新命名與刪除項目。
- **執行檢視**：Timeline、Live debug、Live changes 與 Files 四個工作區視圖，支援桌面與手機版面。
- **Runtime 狀態**：顯示 Codex account、usage limits、模型、reasoning effort 與服務 health。

## 快速開始

### 需求

- Linux
- Python `>=3.12.3,<3.13`
- Poetry 2
- Node.js 與 npm
- 執行服務的 Linux user 已完成 Codex 登入，且 `~/.codex` 可用

### 1. 設定 Project root

建立不進版控的本機設定：

```bash
cp .secrets.toml.example .secrets.toml
```

在 `.secrets.toml` 指定一個已存在、可讀寫的絕對路徑：

```toml
[development]
codex_projects_root = "/home/you/codex-workspaces"
```

root 下每個第一層實體目錄都會成為一個 Project；symbolic link 不會被納入。

### 2. 安裝與建置

```bash
poetry install --with dev
npm ci
npm run tw:build
poetry run alembic upgrade head
```

Python 與前端套件分別由 `poetry.lock`、`package-lock.json` 鎖定。

### 3. 啟動

```bash
poetry run python main.py
```

也可以使用：

```bash
scripts/run.sh
```

啟動後開啟：

- Web console：<http://127.0.0.1:8080>
- OpenAPI：<http://127.0.0.1:8080/docs>

完整服務必須透過 `main.py` 或 `scripts/run.sh` 啟動；直接執行 `uvicorn main:app` 不會初始化 process-level database 與 scheduler。

## systemd 部署

部署專案並完成 production 設定後，註冊、啟動及查看服務狀態：

```bash
sudo scripts/register_systemd.sh
sudo systemctl start agent-app-server.service
sudo systemctl status agent-app-server.service
```

註冊腳本會寫入 `/etc/systemd/system/agent-app-server.service`、執行 `systemctl daemon-reload` 並 enable 服務，但不會自動啟動或重啟服務。

如需指定執行服務的 Linux 使用者：

```bash
sudo AGENT_APP_SERVER_SERVICE_USER=jack scripts/register_systemd.sh
```

如需從不同的專案目錄註冊：

```bash
sudo AGENT_APP_SERVER_APP_DIR=/srv/agent-app-server scripts/register_systemd.sh
```

查看即時服務日誌：

```bash
sudo journalctl -u agent-app-server.service -f
```

## 基本操作

1. 選擇既有 Project，或在 Project selector 建立新目錄。
2. 建立新 Session，或從清單恢復既有 Codex Session。
3. 選擇 model 與 reasoning effort 後送出 prompt。
4. 在 Timeline 查看對話，在 Live debug 觀察事件，在 Live changes 閱讀 diff，或在 Files 管理工作區內容。
5. 長時間工作可用 Inspector 的 Goal 控制項，或輸入 `/goal <objective>`；輸入 `/goal` 可查看目前狀態。

同一個 Session 同時間只允許一個活動 Turn 或 Goal operation；不同 Sessions 可以並行執行。

## 安全與資料邊界

- Codex authentication 沿用服務帳號的 `~/.codex`；本應用不接受或保存 Browser 提交的 API key。
- Project 與 Files API 只接受 server registry 產生的 `project_key` 和 project-relative path，並拒絕 path traversal、absolute path 與 symbolic link。
- Files 分頁可以實際覆寫或刪除工作區內容；部署時應以專用 Linux user 執行，並仔細限制該帳號的檔案權限。
- SQLite 不保存 prompt、agent response、command output、diff、token usage 或 Codex conversation mirror；這些受保護內容由 Project 內權限為 `0700`／`0600` 的 `.stream_journal/` JSONL 保存。
- Trusted Host 檢查不等同 authentication。production 的網路、反向代理、TLS 與身分驗證仍由部署者負責。

## 技術概覽

- Backend：Python 3.12、FastAPI、OpenAI Codex SDK、SQLAlchemy async、SQLite WAL、Alembic、APScheduler
- Frontend：Jinja2、HTMX 2.0.4、Alpine.js 3.14.9、Tailwind CSS 4、Marked、DOMPurify
- Live updates：Stream Journal durable cursor + 原生 `EventSource` SSE fan-out／replay
- Runtime：單一 Uvicorn worker、單一 `AsyncCodex` client

## 開發檢查

```bash
poetry run python -m pytest -q
pipx run ruff check .
npm run tw:build
```

測試使用 fake Codex adapter，不需要真實 Codex login，也不會修改真實 Project。

## 文件

| 文件 | 內容 |
| --- | --- |
| [`docs/operations.md`](docs/operations.md) | 安裝、Dynaconf 設定、權限模式、資料庫、前端、測試、logs 與部署 |
| [`docs/api.md`](docs/api.md) | JSON API、HTML partials、錯誤格式與 SSE endpoint 參考 |
| [`docs/architecture.md`](docs/architecture.md) | 系統邊界、元件責任、資料權責、lifecycle 與單一 worker 限制 |
| [`docs/flows.md`](docs/flows.md) | Project／Files／Thread 操作、Turn、Goal 與 SSE replay／resync 流程 |
| [`docs/session-event-replay.md`](docs/session-event-replay.md) | Stream Journal snapshot cursor、durable replay、fallback 與 resync |

## 術語約定

Codex SDK 與官方 API 將一段對話稱為 **Thread**；Web 前端面向使用者時則統一稱為 **Session**。程式碼、API route 與技術文件保留 Thread／`thread`，以便直接對應 Codex SDK。
