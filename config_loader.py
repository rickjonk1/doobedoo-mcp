"""
Configuratie uit omgevingsvariabelen.

Productie tenzij APP_ENV/FLASK_ENV expliciet development/dev/local is
(fail-closed: een vergeten variabele levert nooit dev-gedrag op).
"""
import os

from dotenv import load_dotenv

load_dotenv()

_env_marker = (os.environ.get("APP_ENV") or os.environ.get("FLASK_ENV") or "").strip().lower()
IS_PRODUCTION = _env_marker not in ("development", "dev", "local")

# SQLite op de versleutelde datadisk; Postgres kan ook via DATABASE_URL.
_raw_db_url = os.environ.get("DATABASE_URL", "sqlite:///doobedoo_mcp.db")
DATABASE_URL = _raw_db_url.replace("postgres://", "postgresql://", 1)
if DATABASE_URL.startswith("postgresql://") and "sslmode" not in DATABASE_URL and IS_PRODUCTION:
    DATABASE_URL += ("&" if "?" in DATABASE_URL else "?") + "sslmode=require"

MERK = os.environ.get("MERK_NAAM", "doobedoo")
PUBLIEKE_URL = os.environ.get("PUBLIEKE_URL", "https://mcp.doobedoo.nl").rstrip("/")
