"""Application configuration store.

This module is the single source of truth for all *application* configuration
(connections, email/SMTP, reminder rules, debug options). Values are persisted
in a TinyDB file inside the data directory and edited through the in-app
Settings page and onboarding wizard rather than environment variables.

Design notes:
- Writes are guarded by a cross-process file lock; reads use an mtime-based
  cache so the two gunicorn workers and the scheduler process all observe each
  other's changes without a heavy locking scheme.
- Environment variables are read ONLY to pre-fill the onboarding wizard so
  existing docker-compose deployments can migrate in one click. They are never
  consulted at runtime once the wizard has been completed.
- Deployment/process settings (FLASK_SECRET_KEY, TRUSTED_PROXIES, REAL_IP_HEADER,
  REDIS_URL, log rotation, gunicorn tuning, DATA_DIR itself) remain environment
  driven and are intentionally NOT part of this schema.
"""

import logging
import os
import secrets
import threading

from filelock import FileLock
from json import JSONDecodeError
from tinydb import TinyDB, Query

try:  # werkzeug ships with Flask; guard so the module still imports without it.
    from werkzeug.security import check_password_hash, generate_password_hash
except Exception:  # pragma: no cover - defensive
    check_password_hash = None
    generate_password_hash = None

logger = logging.getLogger("ForgottenMoviesConfig")

# DATA_DIR is the one setting that must stay environment driven: it is where the
# config file itself lives. Default matches the container bind mount.
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)

CONFIG_DB_PATH = os.path.join(DATA_DIR, "app_config.json")
CONFIG_LOCK_PATH = os.path.join(DATA_DIR, "app_config.lock")

_config_db = TinyDB(CONFIG_DB_PATH)
_ConfigRow = Query()

# Reserved (non-schema) keys stored alongside settings.
_SETUP_COMPLETE_KEY = "__setup_complete"
_ADMIN_USERNAME_KEY = "__admin_username"
_ADMIN_PASSWORD_HASH_KEY = "__admin_password_hash"
_FLASK_SECRET_KEY = "__flask_secret_key"
_UNSUBSCRIBE_SECRET_KEY = "__unsubscribe_secret_key"
_REQUIRE_LOGIN_KEY = "__require_login"

# In-process read cache, refreshed when the backing file's mtime changes.
_CACHE_LOCK = threading.RLock()
_cache: dict[str, object] = {}
_cache_mtime: float | None = None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
# Each field: key, section, label, help, type, default, and flags.
#   type: "text" | "int" | "bool" | "secret" | "enum" | "url" | "email"
#   env:  environment variable used ONLY to pre-fill the wizard (migration aid)
SECTIONS = ["Connections", "Email", "Reminder Rules", "Self-Service", "Debug"]

CONFIG_SCHEMA: list[dict] = [
    # --- Connections ---
    {"key": "OVERSEERR_URL", "section": "Connections", "type": "url", "required": True,
     "label": "Seerr URL", "env": "OVERSEERR_URL",
     "help": "Point at the /api/v1 root, e.g. https://request.example.com/api/v1"},
    {"key": "OVERSEERR_API_KEY", "section": "Connections", "type": "secret", "required": True,
     "label": "Seerr API key", "env": "OVERSEERR_API_KEY", "help": "Found in Seerr settings."},
    {"key": "TAUTULLI_URL", "section": "Connections", "type": "url", "required": True,
     "label": "Tautulli URL", "env": "TAUTULLI_URL",
     "help": "Include the v2 endpoint, e.g. https://tautulli.example.com/api/v2"},
    {"key": "TAUTULLI_API_KEY", "section": "Connections", "type": "secret", "required": True,
     "label": "Tautulli API key", "env": "TAUTULLI_API_KEY", "help": "Found in Tautulli settings."},
    {"key": "THEMOVIEDB_API_KEY", "section": "Connections", "type": "secret", "required": False,
     "label": "TheMovieDB API key", "env": "THEMOVIEDB_API_KEY",
     "help": "Optional. Enables poster artwork in reminder emails."},

    # --- Email ---
    {"key": "SMTP_SERVER", "section": "Email", "type": "text", "required": True,
     "label": "SMTP server", "env": "SMTP_SERVER", "help": "e.g. smtp.gmail.com"},
    {"key": "SMTP_PORT", "section": "Email", "type": "int", "required": False, "default": 587,
     "label": "SMTP port", "env": "SMTP_PORT", "help": "587 for STARTTLS, 465 for SSL."},
    {"key": "SMTP_ENCRYPTION", "section": "Email", "type": "enum", "required": False,
     "default": "STARTTLS", "choices": ["STARTTLS", "SSL", "NONE"],
     "label": "SMTP encryption", "env": "SMTP_ENCRYPTION",
     "help": "STARTTLS (default), SSL (implicit TLS, port 465), or NONE."},
    {"key": "SMTP_USERNAME", "section": "Email", "type": "text", "required": False,
     "label": "SMTP username", "env": "SMTP_USERNAME",
     "help": "Optional. Only if login differs from the From address."},
    {"key": "EMAIL_PASSWORD", "section": "Email", "type": "secret", "required": False,
     "label": "SMTP password", "env": "EMAIL_PASSWORD",
     "help": "Leave blank for relays that accept mail without authentication."},
    {"key": "FROM_EMAIL_ADDRESS", "section": "Email", "type": "email", "required": True,
     "label": "From address", "env": "FROM_EMAIL_ADDRESS", "help": "The reminder sender address."},
    {"key": "FROM_NAME", "section": "Email", "type": "text", "required": False,
     "label": "From name", "env": "FROM_NAME", "help": "Display name on reminder emails."},
    {"key": "BCC_EMAIL_ADDRESS", "section": "Email", "type": "email", "required": False,
     "label": "BCC address", "env": "BCC_EMAIL_ADDRESS",
     "help": "Optional. Receives a copy of every reminder sent."},
    {"key": "ADMIN_NAME", "section": "Email", "type": "text", "required": False,
     "label": "Admin name", "env": "ADMIN_NAME", "help": "Shown in reminder copy as the contact."},
    {"key": "REQUEST_URL", "section": "Email", "type": "url", "required": False,
     "label": "Request portal URL", "env": "REQUEST_URL", "help": "Linked in the email footer."},

    # --- Reminder Rules ---
    {"key": "DAYS_SINCE_REQUEST", "section": "Reminder Rules", "type": "int", "required": False,
     "default": 90, "label": "Days before reminding", "env": "DAYS_SINCE_REQUEST",
     "help": "How long a fulfilled request must sit unwatched before a reminder."},
    {"key": "DAYS_SINCE_REQUEST_EMAIL_TEXT", "section": "Reminder Rules", "type": "text",
     "required": False, "default": "3 months", "label": "Days-since text",
     "env": "DAYS_SINCE_REQUEST_EMAIL_TEXT", "help": "Human phrasing used in the email body."},
    {"key": "HOURS_BETWEEN_EMAILS", "section": "Reminder Rules", "type": "int", "required": False,
     "default": 24, "label": "Hours between emails", "env": "HOURS_BETWEEN_EMAILS",
     "help": "Cooldown per recipient. 168 = once a week."},
    {"key": "HOURS_BETWEEN_EMAILS_EMAIL_TEXT", "section": "Reminder Rules", "type": "text",
     "required": False, "default": "", "label": "Cooldown text",
     "env": "HOURS_BETWEEN_EMAILS_EMAIL_TEXT",
     "help": "Optional human phrasing for the cooldown. Blank auto-derives from the hours."},
    {"key": "OVERSEERR_NUM_OF_HISTORY_RECORDS", "section": "Reminder Rules", "type": "int",
     "required": False, "default": 10, "label": "Seerr records per scan",
     "env": "OVERSEERR_NUM_OF_HISTORY_RECORDS",
     "help": "How many recent fulfilled requests to pull each run."},
    {"key": "TAUTULLI_NEW_REQUEST_METADATA_LIMIT", "section": "Reminder Rules", "type": "int",
     "required": False, "default": 50, "label": "New-title metadata lookups/run",
     "env": "TAUTULLI_NEW_REQUEST_METADATA_LIMIT",
     "help": "Max Tautulli title lookups for brand-new requests per run."},
    {"key": "TAUTULLI_RECENT_UNKNOWN_METADATA_LIMIT", "section": "Reminder Rules", "type": "int",
     "required": False, "default": 30, "label": "Unknown-title refreshes/run",
     "env": "TAUTULLI_RECENT_UNKNOWN_METADATA_LIMIT",
     "help": "Max existing unknown-title refreshes per run."},
    {"key": "JOB_INTERVAL_SECONDS", "section": "Reminder Rules", "type": "int", "required": False,
     "default": 600, "label": "Scan interval (seconds)", "env": "JOB_INTERVAL_SECONDS",
     "help": "How often the scheduler scans. 3600 = hourly."},
    {"key": "INITIAL_DELAY_SECONDS", "section": "Reminder Rules", "type": "int", "required": False,
     "default": 600, "label": "Startup delay (seconds)", "env": "INITIAL_DELAY_SECONDS",
     "help": "Grace period after start before the first scan."},

    # --- Self-Service (unsubscribe) ---
    {"key": "BASE_URL", "section": "Self-Service", "type": "url", "required": False,
     "label": "Public base URL", "env": "BASE_URL",
     "help": "Public HTTPS URL of this instance, e.g. https://forgotten.example.com. "
             "Setting this enables one-click self-service unsubscribe links in emails."},

    # --- Debug ---
    {"key": "DEBUG_MODE", "section": "Debug", "type": "bool", "required": False, "default": False,
     "label": "Debug mode", "env": "DEBUG_MODE",
     "help": "Redirect all mail to the debug address and cap sends per run."},
    {"key": "DEBUG_EMAIL", "section": "Debug", "type": "email", "required": False,
     "label": "Debug address", "env": "DEBUG_EMAIL", "help": "Where debug-mode mail is redirected."},
    {"key": "DEBUG_MAX_EMAILS", "section": "Debug", "type": "int", "required": False, "default": 2,
     "label": "Debug max emails", "env": "DEBUG_MAX_EMAILS", "help": "Send cap per run in debug mode."},
]

_SCHEMA_BY_KEY = {field["key"]: field for field in CONFIG_SCHEMA}


def schema_default(key: str):
    field = _SCHEMA_BY_KEY.get(key)
    if not field:
        return None
    if "default" in field:
        return field["default"]
    return False if field["type"] == "bool" else None


# ---------------------------------------------------------------------------
# Type coercion
# ---------------------------------------------------------------------------
def _coerce(field: dict, value):
    """Coerce a stored/submitted value into the field's declared type."""
    ftype = field["type"]
    if value is None:
        return schema_default(field["key"])
    if ftype == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "on", "yes"}
    if ftype == "int":
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return schema_default(field["key"])
    if ftype == "enum":
        text = str(value).strip().upper()
        return text if text in field.get("choices", []) else schema_default(field["key"])
    # text / secret / url / email
    text = str(value).strip()
    if ftype == "url":
        # Trailing slashes double up when endpoints are appended (".../api/v1//request").
        text = text.rstrip("/")
    return text


# ---------------------------------------------------------------------------
# Low-level storage with mtime cache
# ---------------------------------------------------------------------------
def _current_mtime() -> float | None:
    try:
        return os.path.getmtime(CONFIG_DB_PATH)
    except OSError:
        return None


def _load_cache(force: bool = False) -> dict:
    global _cache, _cache_mtime
    with _CACHE_LOCK:
        mtime = _current_mtime()
        if force or _cache_mtime != mtime or not _cache:
            try:
                # Hold the write lock while reading so we never observe a
                # half-written file from another process mid-save.
                with FileLock(CONFIG_LOCK_PATH, thread_local=False):
                    rows = _config_db.all()
            except JSONDecodeError:
                logger.error("Config store is invalid JSON; treating as empty.")
                rows = []
            _cache = {row["key"]: row.get("value") for row in rows if "key" in row}
            _cache_mtime = mtime
        return dict(_cache)


def _raw(key: str, default=None):
    return _load_cache().get(key, default)


def _write_many(pairs: dict) -> None:
    global _cache_mtime
    with _CACHE_LOCK:
        with FileLock(CONFIG_LOCK_PATH, thread_local=False):
            for key, value in pairs.items():
                _config_db.upsert({"key": key, "value": value}, _ConfigRow.key == key)
        _cache_mtime = None  # force reload on next read
    _load_cache(force=True)


# ---------------------------------------------------------------------------
# Public settings API
# ---------------------------------------------------------------------------
def get(key: str):
    """Effective, type-coerced value for a schema key (DB value or default)."""
    field = _SCHEMA_BY_KEY.get(key)
    if not field:
        return _raw(key)
    return _coerce(field, _raw(key))


def get_all() -> dict:
    """All schema settings as effective values, keyed by schema key."""
    return {field["key"]: get(field["key"]) for field in CONFIG_SCHEMA}


def is_configured(key: str) -> bool:
    """True if a non-empty value has been explicitly stored for this key."""
    value = _raw(key)
    return value not in (None, "")


def save_settings(submitted: dict, *, keep_blank_secrets: bool = True) -> list[str]:
    """Persist a dict of {key: raw_value} for schema keys.

    Returns a list of validation error messages (empty on success).
    For secret fields, a blank submission is ignored when keep_blank_secrets is
    True so the UI can render masked placeholders without wiping stored secrets.
    """
    errors: list[str] = []
    to_write: dict = {}
    for field in CONFIG_SCHEMA:
        key = field["key"]
        if key not in submitted:
            continue
        raw = submitted[key]
        if field["type"] == "secret" and keep_blank_secrets and (raw is None or str(raw).strip() == ""):
            continue  # don't overwrite an existing secret with a blank field
        coerced = _coerce(field, raw)
        if field["type"] == "enum" and str(raw).strip() and coerced is None:
            errors.append(f"{field['label']}: invalid choice.")
            continue
        to_write[key] = coerced
    if errors:
        return errors
    if to_write:
        _write_many(to_write)
    return []


def validate_required() -> list[str]:
    """Return labels of required fields that are not yet configured."""
    missing = []
    for field in CONFIG_SCHEMA:
        if field.get("required") and not is_configured(field["key"]):
            missing.append(field["label"])
    return missing


def env_prefill(key: str) -> str:
    """Value to pre-fill a wizard field with, read from the legacy env var.

    Used only to render the onboarding form; never persisted automatically.
    """
    field = _SCHEMA_BY_KEY.get(key)
    if not field:
        return ""
    env_name = field.get("env")
    if not env_name:
        return ""
    return os.getenv(env_name, "") or ""


# ---------------------------------------------------------------------------
# Setup / onboarding state
# ---------------------------------------------------------------------------
def is_setup_complete() -> bool:
    return bool(_raw(_SETUP_COMPLETE_KEY))


def mark_setup_complete(value: bool = True) -> None:
    _write_many({_SETUP_COMPLETE_KEY: bool(value)})


# ---------------------------------------------------------------------------
# Admin account / auth
# ---------------------------------------------------------------------------
def has_admin() -> bool:
    return bool(_raw(_ADMIN_USERNAME_KEY) and _raw(_ADMIN_PASSWORD_HASH_KEY))


def get_admin_username() -> str | None:
    return _raw(_ADMIN_USERNAME_KEY)


def set_admin(username: str, password: str) -> None:
    if generate_password_hash is None:
        raise RuntimeError("werkzeug is required for password hashing")
    username = (username or "").strip()
    if not username or not password:
        raise ValueError("Username and password are required.")
    _write_many({
        _ADMIN_USERNAME_KEY: username,
        _ADMIN_PASSWORD_HASH_KEY: generate_password_hash(password),
    })


def verify_admin(username: str, password: str) -> bool:
    if check_password_hash is None:
        return False
    stored_user = _raw(_ADMIN_USERNAME_KEY)
    stored_hash = _raw(_ADMIN_PASSWORD_HASH_KEY)
    if not stored_user or not stored_hash:
        return False
    if (username or "").strip() != stored_user:
        return False
    return check_password_hash(stored_hash, password or "")


def is_login_required() -> bool:
    """Whether the dashboard requires a login. Defaults to True (secure)."""
    value = _raw(_REQUIRE_LOGIN_KEY)
    return True if value is None else bool(value)


def set_login_required(value: bool) -> None:
    _write_many({_REQUIRE_LOGIN_KEY: bool(value)})


# ---------------------------------------------------------------------------
# Persisted secrets (auto-generated so they don't need env vars)
# ---------------------------------------------------------------------------
def get_or_create_flask_secret_key() -> str:
    """Return FLASK_SECRET_KEY from env, or a persisted auto-generated key."""
    env_value = os.getenv("FLASK_SECRET_KEY")
    if env_value:
        return env_value
    existing = _raw(_FLASK_SECRET_KEY)
    if existing:
        return existing
    generated = secrets.token_hex(32)
    _write_many({_FLASK_SECRET_KEY: generated})
    return generated


def get_or_create_unsubscribe_secret_key() -> str:
    """Return a stable secret for signing unsubscribe tokens (auto-generated)."""
    env_value = os.getenv("UNSUBSCRIBE_SECRET_KEY")
    if env_value:
        return env_value
    existing = _raw(_UNSUBSCRIBE_SECRET_KEY)
    if existing:
        return existing
    generated = secrets.token_hex(32)
    _write_many({_UNSUBSCRIBE_SECRET_KEY: generated})
    return generated
