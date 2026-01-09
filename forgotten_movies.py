import logging
import requests
import smtplib
import ssl
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from threading import RLock
from tinydb import TinyDB, Query
from tinydb.table import Document
import os
import shutil
from jinja2 import Template, TemplateError
from typing import NamedTuple

TAUTULLI_API_KEY = os.getenv("TAUTULLI_API_KEY")
TAUTULLI_URL = os.getenv("TAUTULLI_URL")
OVERSEERR_API_KEY = os.getenv("OVERSEERR_API_KEY")
OVERSEERR_URL = os.getenv("OVERSEERR_URL")
SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))  # Default to 587 if not set
SMTP_ENCRYPTION_RAW = os.getenv("SMTP_ENCRYPTION")
if SMTP_ENCRYPTION_RAW:
    SMTP_ENCRYPTION = SMTP_ENCRYPTION_RAW.strip().upper()
else:
    SMTP_ENCRYPTION = "SSL" if SMTP_PORT == 465 else "STARTTLS"
if SMTP_ENCRYPTION not in {"STARTTLS", "SSL", "NONE"}:
    raise RuntimeError("SMTP_ENCRYPTION must be one of STARTTLS, SSL, or NONE")
FROM_EMAIL_ADDRESS = os.getenv("FROM_EMAIL_ADDRESS")
FROM_NAME = os.getenv("FROM_NAME")
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
BCC_EMAIL_ADDRESS = os.getenv("BCC_EMAIL_ADDRESS")
OVERSEERR_NUM_OF_HISTORY_RECORDS = int(os.getenv("OVERSEERR_NUM_OF_HISTORY_RECORDS", 10))  # Default 10
ADMIN_NAME = os.getenv("ADMIN_NAME")
THEMOVIEDB_API_KEY = os.getenv("THEMOVIEDB_API_KEY")
DAYS_SINCE_REQUEST = int(os.getenv("DAYS_SINCE_REQUEST", 90))  # Default 90 days
DAYS_SINCE_REQUEST_EMAIL_TEXT = os.getenv("DAYS_SINCE_REQUEST_EMAIL_TEXT", "3 months")
REQUEST_URL = os.getenv("REQUEST_URL")
HOURS_BETWEEN_EMAILS = int(os.getenv("HOURS_BETWEEN_EMAILS", 24))
HOURS_BETWEEN_EMAILS_EMAIL_TEXT = os.getenv("HOURS_BETWEEN_EMAILS_EMAIL_TEXT", f"{HOURS_BETWEEN_EMAILS} hours")
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"
DEBUG_EMAIL = os.getenv("DEBUG_EMAIL")
DEBUG_MAX_EMAILS = int(os.getenv("DEBUG_MAX_EMAILS", 2))


REQUIRED_ENV = {
    "OVERSEERR_URL": OVERSEERR_URL,
    "OVERSEERR_API_KEY": OVERSEERR_API_KEY,
    # ...
}
missing = [name for name, value in REQUIRED_ENV.items() if not value]
if missing:
    raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

# Initialize TinyDB
# Ensure the data directory exists
DATA_DIR = "/app/data"
os.makedirs(DATA_DIR, exist_ok=True)

# Configure logging
LOG_FILE_PATH = os.path.join(DATA_DIR, "forgotten_movies.log")
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
LOG_LEVEL_NAME = os.getenv("LOG_LEVEL", "INFO").upper()
if LOG_LEVEL_NAME not in logging._nameToLevel:
    LOG_LEVEL_NAME = "INFO"
LOG_LEVEL = logging._nameToLevel[LOG_LEVEL_NAME]

logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
logger = logging.getLogger("ForgottenMovies")

root_logger = logging.getLogger()
root_logger.setLevel(LOG_LEVEL)
CURRENT_LOG_LEVEL = LOG_LEVEL

if not any(getattr(handler, "_fm_log_file", False) for handler in root_logger.handlers):
    file_handler = RotatingFileHandler(
        LOG_FILE_PATH,
        maxBytes=int(os.getenv("LOG_FILE_MAX_BYTES", 1_048_576)),
        backupCount=int(os.getenv("LOG_FILE_BACKUP_COUNT", 3)),
    )
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    file_handler._fm_log_file = True  # type: ignore[attr-defined]
    root_logger.addHandler(file_handler)

for handler in root_logger.handlers:
    handler.setLevel(LOG_LEVEL)

BASE_DIR = os.path.dirname(__file__)
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
DEFAULT_EMAIL_TEMPLATE_PATH = os.path.join(TEMPLATES_DIR, "email_template.html")
EMAIL_TEMPLATE_ORIGINAL_PATH = os.path.join(DATA_DIR, "email_template_original.html")
CUSTOM_EMAIL_TEMPLATE_PATH = os.getenv("EMAIL_TEMPLATE_PATH", os.path.join(DATA_DIR, "email_template.html"))
EMAIL_TEMPLATE_CACHE: Template | None = None
EMAIL_TEMPLATE_CACHE_PATH: str | None = None
EMAIL_TEMPLATE_MTIME = None


class SafeDict(dict):
    def __missing__(self, key):
        return ""


def ensure_email_template() -> None:
    try:
        source = DEFAULT_EMAIL_TEMPLATE_PATH
        if not source or not os.path.exists(source):
            raise FileNotFoundError(f"Default email template not found at {source}")
        os.makedirs(os.path.dirname(EMAIL_TEMPLATE_ORIGINAL_PATH), exist_ok=True)
        shutil.copyfile(source, EMAIL_TEMPLATE_ORIGINAL_PATH)
        logger.debug("Refreshed default email template at %s", EMAIL_TEMPLATE_ORIGINAL_PATH)
    except Exception as exc:
        raise RuntimeError(
            f"Unable to prepare email template at {EMAIL_TEMPLATE_ORIGINAL_PATH}: {exc}"
        ) from exc


def _resolve_email_template_path() -> str:
    custom_path = CUSTOM_EMAIL_TEMPLATE_PATH
    if custom_path and os.path.exists(custom_path):
        return custom_path
    return EMAIL_TEMPLATE_ORIGINAL_PATH


def load_email_template() -> Template:
    ensure_email_template()
    template_path = _resolve_email_template_path()
    global EMAIL_TEMPLATE_CACHE, EMAIL_TEMPLATE_MTIME, EMAIL_TEMPLATE_CACHE_PATH
    try:
        mtime = os.path.getmtime(template_path)
        if (
            EMAIL_TEMPLATE_CACHE is None
            or EMAIL_TEMPLATE_MTIME != mtime
            or EMAIL_TEMPLATE_CACHE_PATH != template_path
        ):
            with open(template_path, "r", encoding="utf-8") as handle:
                source = handle.read()
                if not source.strip():
                    raise RuntimeError("Email template is empty.")
                EMAIL_TEMPLATE_CACHE = Template(source)
                EMAIL_TEMPLATE_MTIME = mtime
                EMAIL_TEMPLATE_CACHE_PATH = template_path
        return EMAIL_TEMPLATE_CACHE
    except FileNotFoundError as exc:
        raise RuntimeError(f"Email template not found at {template_path}") from exc
    except TemplateError as exc:
        raise RuntimeError(f"Email template compilation failed: {exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"Failed to load email template: {exc}") from exc


ensure_email_template()


def build_email_body(
    plex_username: str,
    media_type: str,
    title: str,
    plex_url: str | None,
    poster_url: str | None,
    mobile_url: str | None,
) -> str:
    template = load_email_template()

    context = SafeDict(
        plex_username=plex_username,
        media_type=media_type,
        title=title,
        time_since_text=DAYS_SINCE_REQUEST_EMAIL_TEXT,
        plex_url=plex_url or "",
        poster_url=poster_url or "",
        mobile_url=mobile_url or "",
        request_url=REQUEST_URL or "",
        admin_name=ADMIN_NAME or "",
    )

    try:
        return template.render(**context)
    except TemplateError as exc:
        raise RuntimeError(f"Email template formatting failed: {exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"Email template rendering failed: {exc}") from exc


def set_log_level(level_name: str) -> bool:
    """Dynamically adjust logging level for the entire application."""
    level = logging._nameToLevel.get(level_name.upper())
    if level is None:
        return False
    global CURRENT_LOG_LEVEL, LOG_LEVEL_NAME

    CURRENT_LOG_LEVEL = level
    LOG_LEVEL_NAME = level_name.upper()
    root_logger.setLevel(level)
    logger.setLevel(level)
    for handler in root_logger.handlers:
        handler.setLevel(level)
    return True


def get_log_level() -> str:
    """Return the current logging level name."""
    return logging.getLevelName(CURRENT_LOG_LEVEL)


def flush_log_handlers() -> None:
    """Flush all log handlers to ensure data is written to disk."""
    for handler in root_logger.handlers:
        try:
            handler.flush()
        except Exception:
            pass
# Store TinyDB databases in the persistent directory
request_db = TinyDB(os.path.join(DATA_DIR, "request_data.json"))
email_db = TinyDB(os.path.join(DATA_DIR, "email_data.json"))
email_users_db = TinyDB(os.path.join(DATA_DIR, "email_users.json"))
settings_db = TinyDB(os.path.join(DATA_DIR, "settings.json"))

Movie = Query()
Email = Query()
Request = Query()
Setting = Query()
EmailUser = Query()
EMAIL_USER_LOCK = RLock()


def _stable_doc_id(email: str) -> int:
    return (abs(hash(email)) % 2_147_000_000) + 1

SCHEDULER_DISABLED_KEY = "scheduler_disabled"
DEFAULT_SCHEDULER_DISABLED = os.getenv("DISABLE_SCHEDULER", "false").lower() == "true"
LAST_WATCH_STATUS_CHECK_KEY = "last_watch_status_check"

if not settings_db.contains(Setting.key == SCHEDULER_DISABLED_KEY):
    settings_db.insert({"key": SCHEDULER_DISABLED_KEY, "value": DEFAULT_SCHEDULER_DISABLED})


def is_scheduler_disabled() -> bool:
    record = settings_db.get(Setting.key == SCHEDULER_DISABLED_KEY)
    return bool(record and record.get("value"))


def set_scheduler_disabled(value: bool) -> None:
    settings_db.upsert({"key": SCHEDULER_DISABLED_KEY, "value": bool(value)}, Setting.key == SCHEDULER_DISABLED_KEY)


def get_last_watch_status_check() -> datetime | None:
    """Get the timestamp of the last watch status check."""
    record = settings_db.get(Setting.key == LAST_WATCH_STATUS_CHECK_KEY)
    if record and record.get("value"):
        return _parse_iso(record.get("value"))
    return None


def set_last_watch_status_check(timestamp: datetime) -> None:
    """Set the timestamp of the last watch status check."""
    settings_db.upsert(
        {"key": LAST_WATCH_STATUS_CHECK_KEY, "value": timestamp.isoformat()},
        Setting.key == LAST_WATCH_STATUS_CHECK_KEY
    )


def should_run_watch_status_check() -> bool:
    """Check if 24 hours have passed since the last watch status check."""
    last_check = get_last_watch_status_check()
    if last_check is None:
        return True
    time_since_check = datetime.now() - last_check
    return time_since_check >= timedelta(hours=24)


def _parse_iso(value: str | None) -> datetime:
    if not value:
        return datetime.min
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.min


def _resolve_media_added(rec: dict) -> tuple[datetime, str | None]:
    for key in ("mediaAddedDate", "mediaAddedAt", "createdAt"):
        raw = rec.get(key)
        if raw:
            dt = _parse_iso(raw)
            return dt, raw
    return datetime.min, None


def get_overdue_requests_for_ui():
    threshold = datetime.now() - timedelta(days=DAYS_SINCE_REQUEST)
    items = []
    for rec in request_db.all():
        if rec.get("tautulli_watch_date"):
            continue
        if rec.get("email_sent"):
            continue
        if rec.get("skip_email"):
            continue
        email_value = (rec.get("email") or "").strip()
        if email_value and is_unsubscribed(email_value):
            continue
        raw_title = rec.get("title")
        title = raw_title if raw_title not in (None, "") else "Unknown"
        if title == "Unknown":
            continue
        media_dt, media_raw = _resolve_media_added(rec)
        if media_dt == datetime.min or media_dt > threshold:
            continue
        created_display = media_dt.strftime("%Y-%m-%d %H:%M") if media_dt != datetime.min else (media_raw or "")
        items.append(
            {
                "id": rec.get("id"),
                "title": title,
                "media_type": rec.get("mediaType", ""),
                "plex_username": rec.get("plexUsername", ""),
                "email": rec.get("email", ""),
                "created_at_display": created_display,
                "media_added_sort": media_dt.isoformat() if media_dt != datetime.min else (media_raw or ""),
                "_sort": media_dt or datetime.max,
            }
        )
    items.sort(key=lambda item: item["_sort"], reverse=True)
    for item in items:
        item.pop("_sort", None)
    return items


def get_recent_sent_emails(limit: int | None = None):
    records = email_db.all()
    items = []
    for rec in records:
        sent_raw = rec.get("email_sent_at")
        sent_dt = _parse_iso(sent_raw)
        sent_display = sent_dt.strftime("%Y-%m-%d %H:%M") if sent_dt != datetime.min else (sent_raw or "")
        media_raw = rec.get("media_added_at") or rec.get("mediaAddedAt") or rec.get("mediaAddedDate")
        rating_key = rec.get("rating_key")
        if not media_raw and rating_key is not None:
            fallback_query = Request.ratingkey == rating_key
            try:
                int_rating_key = int(rating_key)
            except (TypeError, ValueError):
                int_rating_key = None
            if int_rating_key is not None:
                fallback_query = (Request.ratingkey == rating_key) | (Request.ratingkey == int_rating_key)
            fallback_request = request_db.get(fallback_query)
            if fallback_request:
                _, media_raw_candidate = _resolve_media_added(fallback_request)
                media_raw = media_raw_candidate or fallback_request.get("createdAt")
        media_dt = _parse_iso(media_raw)
        media_display = media_dt.strftime("%Y-%m-%d %H:%M") if media_dt != datetime.min else (media_raw or "")

        watched_raw = rec.get("date_watched")
        watched_dt = _parse_iso(watched_raw)
        watched_display = watched_dt.strftime("%Y-%m-%d %H:%M") if watched_dt != datetime.min else "Unwatched"
        watched_sort = watched_dt.isoformat() if watched_dt != datetime.min else ""

        items.append(
            {
                "title": rec.get("title", "Unknown"),
                "media_type": rec.get("mediaType", ""),
                "plex_username": rec.get("plex_username", ""),
                "email": rec.get("email", ""),
                "email_sent_at_display": sent_display,
                "email_sent_at_sort": sent_dt.isoformat() if sent_dt != datetime.min else (sent_raw or ""),
                "media_added_display": media_display,
                "media_added_sort": media_dt.isoformat() if media_dt != datetime.min else (media_raw or ""),
                "date_watched_display": watched_display,
                "date_watched_sort": watched_sort,
                "_sort": sent_dt or datetime.min,
            }
        )
    items.sort(key=lambda item: item["_sort"], reverse=True)
    if limit:
        items = items[:limit]
    for item in items:
        item.pop("_sort", None)
    return items


def check_unwatched_emails_status() -> dict[str, int]:
    """
    Check all unwatched sent emails to see if users have watched the content.
    Updates date_watched field when content is found in watch history.
    Returns dict with counts of checked, watched, and failed items.
    """
    logger.info("Starting watch status check for unwatched sent emails")

    # Record the timestamp of this check
    set_last_watch_status_check(datetime.now())

    stats = {"checked": 0, "watched": 0, "failed": 0}

    unwatched_emails = [rec for rec in email_db.all() if not rec.get("date_watched")]

    for rec in unwatched_emails:
        stats["checked"] += 1
        plex_username = rec.get("plex_username")
        rating_key = rec.get("rating_key")
        media_type = rec.get("mediaType")
        email = rec.get("email")
        title = rec.get("title", "Unknown")
        tmdb_id = rec.get("tmdbId")

        if not plex_username or not rating_key or not media_type:
            logger.debug("Skipping email record with missing data: %s", rec)
            continue

        try:
            watch_history = has_user_watched_media(plex_username, rating_key, media_type)
            if watch_history:
                # Extract the actual watch date from Tautulli history
                # Tautulli returns a timestamp in seconds, we need to convert to ISO format
                watch_record = watch_history[0]
                watch_timestamp = watch_record.get('stopped') or watch_record.get('date')

                if watch_timestamp:
                    try:
                        watched_at = datetime.fromtimestamp(int(watch_timestamp)).isoformat()
                    except (ValueError, TypeError):
                        # Fallback to current time if timestamp is invalid
                        logger.warning("Invalid timestamp from Tautulli for %s: %s", title, watch_timestamp)
                        watched_at = datetime.now().isoformat()
                else:
                    # Fallback to current time if no timestamp available
                    watched_at = datetime.now().isoformat()

                email_db.update(
                    {"date_watched": watched_at},
                    (Email.email == email) & (Email.tmdbId == str(tmdb_id))
                )
                stats["watched"] += 1
                logger.info(
                    "Marked %s (%s) as watched for %s on %s",
                    title,
                    rating_key,
                    plex_username,
                    watched_at,
                )
        except Exception as exc:
            stats["failed"] += 1
            logger.warning(
                "Failed to check watch status for %s (%s) for user %s: %s",
                title,
                rating_key,
                plex_username,
                exc,
            )

    logger.info(
        "Watch status check complete: checked=%d, watched=%d, failed=%d",
        stats["checked"],
        stats["watched"],
        stats["failed"],
    )
    return stats


def refresh_metadata_for_recent_unknowns(limit: int = 10, pool_size: int = 50) -> dict[str | int, str]:
    updates: dict[str | int, str] = {}
    if limit <= 0 or pool_size <= 0:
        return updates

    threshold_dt = datetime.now() - timedelta(days=DAYS_SINCE_REQUEST)
    candidates = []
    for rec in request_db.all():
        if rec.get("title") not in (None, "", "Unknown"):
            continue
        if not rec.get("ratingkey"):
            continue
        media_dt, _ = _resolve_media_added(rec)
        if media_dt == datetime.min or media_dt > threshold_dt:
            continue
        rec["_media_dt"] = media_dt
        candidates.append(rec)
    candidates.sort(key=lambda rec: rec["_media_dt"], reverse=True)

    for rec in candidates[:pool_size]:
        if len(updates) >= limit:
            break
        request_id = rec.get("id")
        rating_key = rec.get("ratingkey")
        plex_username = rec.get("plexUsername")
        media_type = rec.get("mediaType")
        if not request_id or not rating_key or not plex_username or not media_type:
            continue
        try:
            watch_history = has_user_watched_media(plex_username, rating_key, media_type)
        except Exception as exc:
            logger.warning(
                "Failed to check watch history for request %s (%s): %s",
                request_id,
                plex_username,
                exc,
            )
            continue
        if watch_history:
            title = watch_history[0].get("title", rec.get("title") or "Unknown")
            updates[request_id] = title
            request_db.update(
                {'title': title, 'tautulli_watch_date': datetime.now().isoformat()},
                Request.id == request_id,
            )
            continue
        try:
            metadata = get_tautulli_metadata(rating_key)
            title = metadata.get("title") or rec.get("title") or "Unknown"
            logger.debug("title: %s", title)
        except Exception as exc:
            logger.warning("Failed to refresh metadata for request %s: %s", request_id, exc)
            continue
        updates[request_id] = title
        request_db.update({'title': title}, Request.id == request_id)
    for rec in candidates:
        rec.pop("_media_dt", None)
    return updates

# Fetch Overseerr requests
def get_overseerr_requests():
    response = requests.get(f"{OVERSEERR_URL}/request?take={OVERSEERR_NUM_OF_HISTORY_RECORDS}&filter=available&sort=added", headers={"X-Api-Key": OVERSEERR_API_KEY})
    response.raise_for_status()
    return response.json()['results']

def _check_overseerr_connection(timeout=(5, 15)) -> bool:
    if not OVERSEERR_URL or not OVERSEERR_API_KEY:
        logger.error("OVERSEERR CONNECTION FAILED: missing OVERSEERR_URL or OVERSEERR_API_KEY.")
        return False
    test_url = f"{OVERSEERR_URL}/request"
    params = {"take": 1, "filter": "available", "sort": "added"}
    headers = {"X-Api-Key": OVERSEERR_API_KEY}
    try:
        resp = requests.get(test_url, params=params, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        logger.error("OVERSEERR CONNECTION FAILED: %s", exc)
        return False

def _check_tautulli_connection(timeout=(5, 15)) -> bool:
    if not TAUTULLI_URL or not TAUTULLI_API_KEY:
        logger.error("TAUTULLI CONNECTION FAILED: missing TAUTULLI_URL or TAUTULLI_API_KEY.")
        return False
    params = {'apikey': TAUTULLI_API_KEY, 'cmd': 'get_server_info'}
    try:
        resp = requests.get(TAUTULLI_URL, params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if data.get('response', {}).get('result') != 'success':
            logger.error("TAUTULLI CONNECTION FAILED: unexpected response %s", data)
            return False
        return True
    except requests.RequestException as exc:
        logger.error("TAUTULLI CONNECTION FAILED: %s", exc)
        return False
    except ValueError as exc:
        logger.error("TAUTULLI CONNECTION FAILED: invalid JSON response (%s)", exc)
        return False

def run_startup_checks() -> bool:
    logger.info("Running startup connectivity checks.")
    overseerr_ok = _check_overseerr_connection()
    tautulli_ok = _check_tautulli_connection()
    if overseerr_ok and tautulli_ok:
        logger.info("All external services reachable.")
    return overseerr_ok and tautulli_ok

def get_tmdb_poster(tmdb_id, media_type):
    url = f"https://api.themoviedb.org/3/{'movie' if media_type == 'movie' else 'tv'}/{tmdb_id}"
    try:
        resp = requests.get(url, params={"api_key": THEMOVIEDB_API_KEY}, timeout=(5, 15))
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        logger.warning("TMDB request timed out for %s (%s)", tmdb_id, media_type)
        return ""
    except requests.exceptions.RequestException as exc:
        logger.warning("TMDB request failed for %s (%s): %s", tmdb_id, media_type, exc)
        return ""
    data = resp.json()
    return f"https://image.tmdb.org/t/p/w500{data.get('poster_path', '')}" if data.get("poster_path") else ""

# Check Tautulli watch history for a specific user and media
def has_user_watched_media(user, rating_key, media_type):
    params = {
        'apikey': TAUTULLI_API_KEY,
        'cmd': 'get_history',
        'user': user,
        'length': 1
    }
    if media_type == 'tv show':
        params['grandparent_rating_key'] = rating_key
    else:
        params['rating_key'] = rating_key
    response = requests.get(TAUTULLI_URL, params=params)
    response.raise_for_status()
    watch_history = response.json()['response']['data']['data']
    if DEBUG_MODE:
        logger.debug("watch_history: %s", watch_history)
        
    return watch_history

# Get metadata from Tautulli
def get_tautulli_metadata(rating_key):
    # Fetch metadata from Tautulli
    params = {
        'apikey': TAUTULLI_API_KEY,
        'cmd': 'get_metadata',
        'rating_key': rating_key
    }
    response = requests.get(TAUTULLI_URL, params=params)
    response.raise_for_status()
    metadata = response.json()['response']['data']
    return metadata

EMAIL_USER_DEFAULTS = {
    'last_email_at': None,
    'next_email_at': None,
    'unsubscribed_at': None,
}


def _ensure_email_user_record(email: str) -> dict | None:
    if not email:
        return None
    normalized = email.lower()
    with EMAIL_USER_LOCK:
        record = email_users_db.get(EmailUser.email == normalized)
        if record is None:
            base_doc_id = _stable_doc_id(normalized)
            doc_id = base_doc_id
            existing_doc = email_users_db.get(doc_id=doc_id)
            while existing_doc and existing_doc.get('email') != normalized:
                doc_id += 1
                if doc_id >= 2_147_483_647:
                    doc_id = 1
                if doc_id == base_doc_id:
                    raise RuntimeError("Unable to allocate unique doc_id for email_users")
                existing_doc = email_users_db.get(doc_id=doc_id)

            payload = {'email': normalized}
            payload.update(EMAIL_USER_DEFAULTS)
            email_users_db.upsert(Document(payload, doc_id=doc_id), EmailUser.email == normalized)
            record = email_users_db.get(doc_id=doc_id)
            logger.info("Registered email user %s", normalized)
        updates: dict[str, object] = {}
        for key, default in EMAIL_USER_DEFAULTS.items():
            if key not in record:
                updates[key] = default
        if updates:
            email_users_db.update(updates, EmailUser.email == normalized)
            record = email_users_db.get(EmailUser.email == normalized)
        if record is None:
            raise RuntimeError(f"Email user record vanished for {normalized}")
        return record


def is_unsubscribed(email):
    if not email:
        return False
    record = _ensure_email_user_record(email)
    if not record:
        return False
    unsubscribed_at = record.get('unsubscribed_at')
    return bool(unsubscribed_at and _parse_iso(unsubscribed_at) != datetime.min)


def add_unsubscribed_email(email):
    if not email:
        return
    with EMAIL_USER_LOCK:
        record = _ensure_email_user_record(email)
        if not record:
            return
        timestamp = datetime.now().isoformat()
        email_users_db.update(
            {'unsubscribed_at': timestamp},
            EmailUser.email == record['email'],
        )
        logger.info("Marked %s as unsubscribed at %s", record['email'], timestamp)


def remove_unsubscribed_email(email):
    if not email:
        return False
    with EMAIL_USER_LOCK:
        record = _ensure_email_user_record(email)
        if not record or not record.get('unsubscribed_at'):
            return False
        email_users_db.update({'unsubscribed_at': None}, EmailUser.email == record['email'])
        logger.info("Removed %s from unsubscribe list", record['email'])
        return True


def list_unsubscribed_emails():
    with EMAIL_USER_LOCK:
        records = [dict(rec) for rec in email_users_db.all() if rec.get('unsubscribed_at')]
    return sorted(records, key=lambda item: item.get('email', ''))


def get_email_user(email: str) -> dict | None:
    if not email:
        return None
    record = _ensure_email_user_record(email)
    return record if record else None


def mark_email_user(email: str, last_email_at: datetime) -> None:
    if not email:
        return
    with EMAIL_USER_LOCK:
        record = _ensure_email_user_record(email)
        if not record:
            return
        next_email_at = last_email_at + timedelta(hours=HOURS_BETWEEN_EMAILS)
        email_users_db.update(
            {
                'last_email_at': last_email_at.isoformat(),
                'next_email_at': next_email_at.isoformat(),
            },
            EmailUser.email == record['email'],
        )


class SendOutcome(NamedTuple):
    sent: bool
    remove_candidate: bool
    message: str
    title: str
    recipient: str | None
    sent_at: datetime | None


def _attempt_send_request(
    record: dict,
    metadata_updates: dict | None,
    *,
    user_record: dict | None,
    respect_cycle: bool,
    respect_cooldown: bool,
    perform_db_updates: bool,
    allow_sleep: bool,
    now_dt: datetime | None = None,
) -> SendOutcome:
    now_dt = now_dt or datetime.now()
    request_id = record.get('id')
    email_value = (record.get('email') or '').strip()
    plex_username = record.get('plexUsername')
    rating_key = record.get('ratingkey')
    media_type = record.get('mediaType')
    plex_url = record.get('plexUrl')
    mobile_url = record.get('mobilePlexUrl')
    tmdb_id = record.get('tmdbId')
    poster_url = record.get('posterUrl', '')
    _, media_raw = _resolve_media_added(record)
    title_lookup = (metadata_updates or {}).get(request_id)
    title = title_lookup if title_lookup else (record.get('title') or "Unknown")

    if not email_value:
        logger.warning("Skipping request %s; missing email address.", request_id)
        return SendOutcome(False, True, "Missing email address for request.", title, None, None)

    if respect_cycle and not record.get('eligible_for_email', False):
        request_db.update({'eligible_for_email': True}, Request.id == request_id)
        logger.info("Deferring email for %s; waiting one scheduler cycle.", email_value)
        return SendOutcome(False, False, "Waiting one scheduler cycle before emailing.", title, None, None)

    if is_unsubscribed(email_value):
        logger.info("Skipping email to %s for %s (%s); address is unsubscribed.", email_value, title, rating_key)
        return SendOutcome(False, False, "Address is unsubscribed; reminder not sent.", title, None, None)

    watch_history = has_user_watched_media(plex_username, rating_key, media_type)
    if watch_history:
        watched_title = watch_history[0].get('title', title)
        request_db.update({'tautulli_watch_date': datetime.now().isoformat()}, Request.id == request_id)
        logger.info("Marking %s as watched for %s; no reminder sent.", watched_title, email_value)
        return SendOutcome(False, True, f"{watched_title} already appears watched; reminder not sent.", title, None, None)

    if title == "Unknown":
        metadata = get_tautulli_metadata(rating_key)
        title = metadata.get('title') or title
        request_db.update({'title': title}, Request.id == request_id)

    email_record = email_db.search((Email.email == email_value) & (Email.tmdbId == str(tmdb_id)))
    if email_record:
        logger.info("Skipping email to %s for %s (already notified).", email_value, title)
        request_db.update({'email_sent': True}, Request.id == request_id)
        return SendOutcome(False, True, "Reminder already sent for this title.", title, None, None)

    effective_user_record = user_record or get_email_user(email_value)
    if respect_cooldown and effective_user_record:
        next_allowed = _parse_iso(effective_user_record.get('next_email_at'))
        if next_allowed != datetime.min and now_dt < next_allowed:
            logger.info(
                "Skipping email to %s; next reminder allowed at %s.",
                email_value,
                next_allowed.strftime("%Y-%m-%d %H:%M"),
            )
            return SendOutcome(False, False, f"Cooldown active until {next_allowed.strftime('%Y-%m-%d %H:%M')}.", title, None, None)

    if respect_cooldown and (not effective_user_record or not effective_user_record.get('last_email_at')):
        legacy_last_email = email_db.search((Email.email == email_value))
        if legacy_last_email:
            last_sent_at = datetime.fromisoformat(legacy_last_email[0]['email_sent_at'])
            if now_dt - last_sent_at < timedelta(hours=HOURS_BETWEEN_EMAILS):
                logger.info(
                    "Skipping email to %s as it was sent within the last %s hours.",
                    email_value,
                    HOURS_BETWEEN_EMAILS,
                )
                return SendOutcome(False, False, "Reminder recently sent; cooldown in effect.", title, None, None)

    email_subject = f"Plex Reminder: {title} is available and unwatched"
    email_body = build_email_body(
        plex_username=plex_username,
        media_type=media_type,
        title=title,
        plex_url=plex_url,
        poster_url=poster_url,
        mobile_url=mobile_url,
    )

    if DEBUG_MODE:
        logger.debug(
            "Preparing email send for request %s: recipient=%s, subject=%s, rating_key=%s, media_type=%s",
            request_id,
            email_value,
            email_subject,
            rating_key,
            media_type,
        )
    try:
        recipient = send_email(email_value, email_subject, email_body, is_html=True)
    except Exception:
        logger.exception(
            "Email send failed for %s (%s) [request %s, rating_key=%s].",
            email_value,
            title,
            request_id,
            rating_key,
        )
        raise
    logger.info(
        "Sent email to %s (%s) via %s for %s (%s).",
        plex_username,
        email_value,
        recipient,
        title,
        rating_key,
    )
    sent_at = datetime.now()

    if perform_db_updates:
        request_db.update({'email_sent': True, 'title': title}, Request.id == request_id)
        mark_email_user(email_value, sent_at)
        email_db.upsert(
            {
                'rating_key': str(rating_key),
                'tmdbId': str(tmdb_id),
                'email': email_value,
                'plex_username': plex_username,
                'title': title,
                'poster_url': poster_url,
                'mediaType': media_type,
                'media_added_at': media_raw or record.get('createdAt'),
                'email_sent_at': sent_at.isoformat(),
                'date_watched': None
            },
            (Email.email == email_value) & (Email.tmdbId == str(tmdb_id))
        )
        if allow_sleep:
            time.sleep(3)
    else:
        logger.info("Debug mode active; skipped persisting send for %s.", email_value)

    return SendOutcome(True, True, f"Sent reminder for {title} to {recipient}.", title, recipient, sent_at)



# Transform Plex URL
def transform_plex_url(plex_url):
    # Bail out early when Overseerr hasn't populated a Plex URL yet.
    if not plex_url:
        return None, None

    # Replace #! with web/index.html#! for browser link
    browser_url = plex_url

    # Construct a mobile-friendly Plex link using regex to extract server and key details
    import re
    match = re.search(r'/server/([^/]+)/details\?key=([^&]+)', plex_url)
    if match:
        server_id, metadata_key = match.groups()
        mobile_url = f"plex://server/{server_id}/details?key={metadata_key}"
        return browser_url, mobile_url
    return browser_url, None
    
    
# Send email notification
def send_email(to_address, subject, body, is_html=False):
    if DEBUG_MODE:
        logger.debug(
            "send_email invoked for %s (subject=%s, html=%s).",
            to_address,
            subject,
            is_html,
        )
    if is_html:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "html"))
    else:
        msg = MIMEText(body)

    msg['Subject'] = subject
    auth_user = (SMTP_USERNAME or "").strip() or FROM_EMAIL_ADDRESS
    from_address = (FROM_EMAIL_ADDRESS or "").strip() or auth_user or ""
    if FROM_NAME:
        msg['From'] = f"{FROM_NAME} <{from_address}>"
    else:
        msg['From'] = from_address
    # When debugging, redirect the email to ourselves and avoid contacting watchers.
    actual_recipient = to_address
    if DEBUG_MODE:
        actual_recipient = DEBUG_EMAIL or FROM_EMAIL_ADDRESS
        logger.info("Debug email mode enabled; redirecting email originally for %s to %s.", to_address, actual_recipient)
        msg['To'] = actual_recipient
        msg['X-Debug-Original-To'] = to_address
    else:
        msg['To'] = actual_recipient
        msg['Bcc'] = BCC_EMAIL_ADDRESS

    if DEBUG_MODE:
        logger.debug(
            "Connecting to SMTP server %s:%s as %s using %s.",
            SMTP_SERVER,
            SMTP_PORT,
            auth_user,
            SMTP_ENCRYPTION,
        )
    try:
        ssl_context = ssl.create_default_context()
        if SMTP_ENCRYPTION == "SSL":
            smtp_conn = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, context=ssl_context)
        else:
            smtp_conn = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        with smtp_conn as server:
            if SMTP_ENCRYPTION == "STARTTLS":
                if DEBUG_MODE:
                    logger.debug("SMTP connection established, issuing STARTTLS.")
                server.starttls(context=ssl_context)
                if DEBUG_MODE:
                    logger.debug("STARTTLS negotiation succeeded; logging in.")
            elif SMTP_ENCRYPTION == "SSL":
                if DEBUG_MODE:
                    logger.debug("Implicit SSL/TLS connection established; logging in.")
            else:
                if DEBUG_MODE:
                    logger.debug("SMTP encryption disabled per configuration; logging in without TLS.")
            if auth_user and EMAIL_PASSWORD:
                server.login(auth_user, EMAIL_PASSWORD)
                if DEBUG_MODE:
                    logger.debug("SMTP login succeeded; sending message to %s.", actual_recipient)
            elif DEBUG_MODE:
                logger.debug("SMTP credentials missing; sending without AUTH to %s.", actual_recipient)
            server.send_message(msg)
            if DEBUG_MODE:
                try:
                    server.noop()
                    logger.debug("SMTP NOOP succeeded after send.")
                except smtplib.SMTPException as exc:
                    logger.debug("SMTP NOOP failed post-send (non-fatal): %s", exc)
    except smtplib.SMTPException as exc:
        logger.error(
            "SMTP error while sending to %s (subject=%s): %s",
            actual_recipient,
            subject,
            exc,
        )
        raise
    except Exception as exc:
        logger.error(
            "Unexpected error while sending to %s (subject=%s): %s",
            actual_recipient,
            subject,
            exc,
        )
        raise
    if DEBUG_MODE:
        logger.debug("Email sent successfully to %s.", actual_recipient)
    return actual_recipient

# Main logic
def main():
    if not _check_overseerr_connection():
        logger.info("Aborting run due to Overseerr connectivity issues.")
        return
    if not _check_tautulli_connection():
        logger.info("Aborting run due to Tautulli connectivity issues.")
        return

    logger.info("Step 1: Check watch status for unwatched sent emails")
    if should_run_watch_status_check():
        try:
            check_unwatched_emails_status()
        except Exception as exc:
            logger.exception("Watch status check failed: %s", exc)
    else:
        last_check = get_last_watch_status_check()
        if last_check:
            time_since = datetime.now() - last_check
            hours_remaining = 24 - (time_since.total_seconds() / 3600)
            logger.info(
                "Skipping watch status check (last run: %s, %.1f hours until next check)",
                last_check.strftime("%Y-%m-%d %H:%M:%S"),
                hours_remaining
            )
        else:
            logger.info("Skipping watch status check (timestamp tracking issue)")

    logger.info("Step 2: Grab requests from Overseerr")
    # Step 1: Fetch new Overseerr requests and add them to the database if not already present
    overseerr_requests = get_overseerr_requests()
    debug_emails_sent = 0
    # print(f"overseerr_requests:")
    # print(overseerr_requests)

    for request in overseerr_requests:
        if DEBUG_MODE:
            logger.debug("for request in overseerr_requests: %s", request)
        request_id = request['id']
        requested_by_email_raw = request['requestedBy']['email']
        requested_by_email = (requested_by_email_raw or "").strip()
        _ensure_email_user_record(requested_by_email)

        if not request_db.search(Request.id == request_id):
            media = request['media']
            
            media_added_raw = request['media'].get('mediaAddedAt') or request['media'].get('mediaAddedDate')
            if media_added_raw:
                media_added_clean = media_added_raw.rstrip('Z')
                try:
                    media_added_dt = datetime.fromisoformat(media_added_clean)
                except ValueError:
                    media_added_dt = datetime.now()
            else:
                media_added_dt = datetime.now()
            created_now_iso = datetime.now().isoformat()
            tmdb_id = request['media']['tmdbId']
            ratingkey = request['media']['ratingKey']
            media_type = 'movie' if request['media']['mediaType'] == 'movie' else 'tv show'
            requested_by_username = request['requestedBy']['plexUsername']
            # Resolve Plex URLs (new Overseerr fields + backward compatibility)
            raw_plex_url = media.get('plexUrl') or media.get('mediaUrl')
            mobile_url = media.get('iOSPlexUrl')
            
            plex_url, fallback_mobile = transform_plex_url(raw_plex_url)
            mobile_url = mobile_url or fallback_mobile
            # Fetch poster URL from TMDB and add it to the database
            poster_url = get_tmdb_poster(tmdb_id, media_type)

            request_db.insert({
                'id': request_id,
                'mediaAddedDate': media_added_dt.isoformat(),
                'createdAt': created_now_iso,
                'tmdbId': str(tmdb_id),
                'ratingkey': ratingkey,
                'mediaType': media_type,
                'plexUsername': requested_by_username,
                'email': requested_by_email,
                'plexUrl': plex_url,
                'mobilePlexUrl': mobile_url,
                'posterUrl': poster_url,
                'tautulli_watch_date': None,
                'email_sent': False,
                'skip_email': False,
                'eligible_for_email': False,
                'title': "Unknown"
            })

    # Ensure email user records exist for any legacy requests
    for existing_request in request_db.all():
        existing_email = (existing_request.get('email') or '').strip()
        if existing_email:
            _ensure_email_user_record(existing_email)

    # Step 3: Refresh metadata for recent unknown titles
    logger.info("Step 3: Update 10 recent titles from Tautulli")
    metadata_updates = refresh_metadata_for_recent_unknowns(limit=10, pool_size=50)

    # Step 4: Evaluate reminders per user
    threshold_dt = datetime.now() - timedelta(days=DAYS_SINCE_REQUEST)
    overdue_by_email: dict[str, list[tuple[datetime, dict]]] = {}
    for rec in request_db.all():
        if rec.get("tautulli_watch_date"):
            continue
        if rec.get("email_sent"):
            continue
        if rec.get("skip_email"):
            continue
        media_dt, _ = _resolve_media_added(rec)
        if media_dt == datetime.min or media_dt > threshold_dt:
            continue
        email_value = (rec.get('email') or '').strip()
        if not email_value:
            continue
        title_value = rec.get('title') or "Unknown"
        if title_value in (None, "", "Unknown"):
            continue
        email_key = email_value.lower()
        overdue_by_email.setdefault(email_key, []).append((media_dt, rec))

    with EMAIL_USER_LOCK:
        user_records_snapshot = [dict(rec) for rec in email_users_db.all()]
    now_dt = datetime.now()
    for user_record in user_records_snapshot:
        email_value = (user_record.get('email') or '').strip()
        if not email_value:
            continue
        if user_record.get('unsubscribed_at'):
            continue
        email_key = email_value.lower()
        candidates = overdue_by_email.get(email_key)
        if not candidates:
            continue

        user_record = _ensure_email_user_record(email_value)
        if not user_record:
            continue

        while candidates:
            if DEBUG_MODE and debug_emails_sent >= DEBUG_MAX_EMAILS:
                logger.info("Debug mode email limit of %s reached; stopping run.", DEBUG_MAX_EMAILS)
                return

            _, request = candidates[0]
            outcome = _attempt_send_request(
                request,
                metadata_updates,
                user_record=user_record,
                respect_cycle=True,
                respect_cooldown=True,
                perform_db_updates=not DEBUG_MODE,
                allow_sleep=not DEBUG_MODE,
                now_dt=now_dt,
            )

            if not outcome.sent:
                if outcome.remove_candidate:
                    candidates.pop(0)
                    continue
                break

            if DEBUG_MODE:
                debug_emails_sent += 1
                logger.info(
                    "Skipped persisting send for %s (%s) due to debug mode.",
                    request.get('plexUsername'),
                    request.get('ratingkey'),
                )
                if debug_emails_sent >= DEBUG_MAX_EMAILS:
                    logger.info("Debug mode sent %s emails; exiting early.", debug_emails_sent)
                    return

            candidates.pop(0)
            break

if __name__ == "__main__":
    main()
