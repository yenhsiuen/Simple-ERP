"""
程式設定：資料庫連線與店名。

家裡使用的設定（資料庫密碼、店名等）放在同資料夾的 .env 檔，不會上傳到 GitHub。
沒有 .env 時會使用下方的預設值（展示用的 erp_demo 資料庫）。
"""
import os

def load_env_file(path):
    """讀取 .env 檔（KEY=VALUE 格式），不需要額外安裝套件"""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "erp_demo")
APP_NAME = os.getenv("APP_NAME", "範例商行")
