"""
Pytest 會在收集 test modules 前自動載入此檔案，無需手動 import。這是 pytest 的自動載入機制。

  執行：
  pytest tests/test_app.py

  pytest 會依序：
  1. 搜尋 tests/ 及其父目錄中的 conftest.py。
  2. 載入並執行 tests/conftest.py 的 module-level 程式碼：
      - 設定 ENV_FOR_DYNACONF=development
      - 建立臨時 DB 路徑
      - 設定 DATABASE_URL

  3. 接著才 import tests/test_app.py。
  4. test_app.py 執行 from main import app。
  5. main.py 再 import config.py 和 database.py，此時它們讀到的已經是測試環境變數。
  6. 測試開始前，pytest 自動執行 database_runtime fixture，因為它設定了 autouse=True。
  7. 所有測試結束後執行 fixture 的 teardown，釋放 database engine。

  pytest
    └─ 載入 tests/conftest.py
         ├─ ENV_FOR_DYNACONF=development
         └─ DATABASE_URL=臨時 DB
             └─ import test_app.py
                  └─ import main.py
                       ├─ import config.py
                       └─ import database.py

  database_runtime 不必出現在 test function 參數中，因為：

  @pytest.fixture(scope="session", autouse=True)

  - autouse=True：自動套用。
  - scope="session"：整次 pytest 只初始化和清理一次。
"""

import asyncio
import os
from pathlib import Path
import tempfile

import pytest


# 必須在 test_app.py import main/config/database 前選定 Dynaconf environment，
# 並將 database engine 指向隔離的臨時 DB，避免污染正式資料。
os.environ["ENV_FOR_DYNACONF"] = "development"
os.environ["DYNACONF_CODEX_ENABLED"] = "false"

_test_directory = tempfile.TemporaryDirectory(prefix="se-tests-")
_database_path = Path(_test_directory.name) / "test.db"
_projects_root = Path(_test_directory.name) / "projects"
_projects_root.mkdir()
(_projects_root / "agent_app_server").mkdir()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_database_path}"
os.environ["DYNACONF_CODEX_PROJECTS_ROOT"] = str(_projects_root)


@pytest.fixture(scope="session", autouse=True)
def database_runtime():
    """整次 pytest session 自動初始化一次測試 DB，結束後釋放 engine。"""
    from database import dispose_engine, init_db

    asyncio.run(init_db())
    yield
    asyncio.run(dispose_engine())
