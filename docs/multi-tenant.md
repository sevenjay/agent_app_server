# Keycloak、oauth2-proxy 與多租戶部署

Agent App Server 不實作登入畫面、密碼驗證、OIDC callback 或 Browser session。Keycloak 管理使用者，oauth2-proxy 完成 OIDC Authorization Code flow 並把已驗證的身分放進 upstream headers。

```text
Browser --HTTPS--> oauth2-proxy --OIDC--> Keycloak
                         |
                         | trusted identity headers
                         v
                 Agent App Server --shared account--> Codex
```

## Keycloak client

在專用 realm 中建立 confidential OpenID Connect client：

- Client ID：`agent-app-server`
- Client authentication：啟用
- Standard flow：啟用
- Direct access grants、implicit flow、service accounts：停用
- Valid redirect URI：`https://console.example.com/oauth2/callback`
- Web origin：`https://console.example.com`
- Scopes：`openid profile email`
- PKCE：`S256`

可匯入 [`deploy/keycloak/agent-app-server-client.example.json`](../deploy/keycloak/agent-app-server-client.example.json) 後修改 hostname 並重新產生 client secret。不要把實際 secret 提交到版本庫。

Keycloak username 必須能轉成小寫後符合 `^[a-z0-9][a-z0-9._@-]{0,127}$`。`preferred_username` 用來命名首次建立的 workspace directory；oauth2-proxy 的 user identity header 則作為穩定 owner。若 identity header 的值改變，系統會把它視為新使用者。

## oauth2-proxy

複製 [`deploy/oauth2-proxy/oauth2-proxy.cfg.example`](../deploy/oauth2-proxy/oauth2-proxy.cfg.example)，修改 issuer、console URL 與 upstream。透過環境變數提供：

```bash
export OAUTH2_PROXY_CLIENT_SECRET='<keycloak-client-secret>'
export OAUTH2_PROXY_COOKIE_SECRET='<oauth2-proxy-cookie-secret>'
oauth2-proxy --config ./deploy/oauth2-proxy/oauth2-proxy.cfg
```

直接 upstream proxy 模式會使用：

- `X-Forwarded-User`：穩定 external identity。
- `X-Forwarded-Preferred-Username`：Keycloak account name。

若部署採 Nginx `auth_request` 模式，oauth2-proxy 通常回傳 `X-Auth-Request-*`；此時必須讓 Nginx 明確覆寫送給應用程式的 headers，並同步修改本專案的 header 名稱設定。

oauth2-proxy 必須移除或覆寫 Browser 傳入的同名 headers。Agent App Server 的 port 只能由 oauth2-proxy／內部 reverse proxy 存取；使用者若能直接連到 backend，就能偽造身分。

## Agent App Server

先建立 service account 可讀寫、但不公開給其他 OS users 的 workspace root：

```bash
install -d -m 0700 /srv/codex-workspaces
```

設定 `.secrets.toml`：

```toml
[production]
deployment_mode = "multi_tenant"
codex_projects_root = "/srv/codex-workspaces"
oauth2_proxy_subject_header = "X-Forwarded-User"
oauth2_proxy_username_header = "X-Forwarded-Preferred-Username"
web_host = "127.0.0.1"
trusted_hosts = ["console.example.com"]
```

套用 database migration 後啟動：

```bash
poetry run alembic upgrade head
poetry run python main.py
```

使用者第一次通過驗證時會建立：

```text
/srv/codex-workspaces/<username>/
```

該目錄及系統新建的 Project 使用 `0700`。每個 username 下面的第一層實體目錄是該使用者的 Codex Projects；symlink 不會被註冊。

詳細 `/api/status` 維持不需登入，可能包含共用 Codex account 與 runtime 資訊。Projects、Files、Threads、Turns、Goals、SSE、partials 與 preferences 都要求 proxy identity。

## 單用戶相容模式

```toml
[production]
deployment_mode = "single_user"
codex_projects_root = "/srv/codex-workspaces"
```

此模式不讀取或要求 proxy headers，Project 路徑維持 `/srv/codex-workspaces/<project>`。

從單用戶改成多租戶前，必須停機並明確把原有 Projects 移到指定帳號目錄；應用程式不會猜測既有資料的 owner。SQLite 的既有 UI metadata 會在 migration 時歸到 `local-service-user`，不會自動轉給任何 Keycloak subject。

## 隔離限制

這是 workspace 與 Web authorization scope，不是安全沙箱：

- 所有租戶共用一個 Linux service user、Codex login、模型與 usage limit。
- Codex processes 及 filesystem permissions 沒有依租戶分離。
- 嚴格的不互信租戶需要不同 UID、container/process、filesystem mount 與 Codex home。
