import ipaddress
import json
import logging
import os
import time
import uuid
from datetime import datetime
from functools import wraps
from threading import Thread

from flask import (
    Flask,
    flash,
    get_flashed_messages,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from flask_limiter import Limiter

import config_store
import forgotten_movies as fm
from config_store import (
    CONFIG_SCHEMA,
    SECTIONS,
    env_prefill,
    has_admin,
    is_login_required,
    is_setup_complete,
    mark_setup_complete,
    save_settings,
    set_admin,
    set_login_required,
    validate_required,
    verify_admin,
)
from forgotten_movies import (
    APP_VERSION,
    add_unsubscribed_email,
    check_unwatched_emails_status,
    flush_log_handlers,
    get_email_user,
    get_job_status,
    get_log_level,
    get_overseerr_requests,
    get_overseerr_requests_page,
    get_overdue_requests_for_ui,
    get_recent_sent_emails,
    get_user_stats,
    is_scheduler_disabled,
    list_unsubscribed_emails,
    LOG_FILE_PATH,
    remove_unsubscribed_email,
    request_db,
    Request,
    refresh_metadata_for_recent_unknowns,
    send_email,
    set_scheduler_disabled,
    set_log_level,
    test_overseerr_connection,
    test_tautulli_connection,
    get_email_template_source,
    get_default_email_template_source,
    is_using_custom_email_template,
    save_email_template_source,
    reset_email_template,
    render_email_preview,
    EMAIL_TEMPLATE_VARIABLES,
    _attempt_send_request,
    _decrypt_email,
    build_resubscribe_url,
)
from filelock import Timeout

from job_runner import acquire_job_lock, execute_job

APP_LOGGER = logging.getLogger("ForgottenMoviesWeb")
APP_LOGGER.setLevel(logging.INFO)
BACKGROUND_TASKS: dict[str, dict[str, str]] = {}

# Endpoints reachable without authentication or completed setup. Token-based
# unsubscribe pages are used by email recipients, not the admin, so they stay
# public; static assets and the health check are always public.
PUBLIC_ENDPOINTS = {
    "login",
    "static",
    "asset",
    "favicon",
    "health",
    "unsubscribe_via_link",
    "resubscribe_via_link",
}

# Endpoints reachable during first-run onboarding (before an admin exists).
SETUP_ENDPOINTS = {"setup", "test_connection", "send_test_email"}

app = Flask(__name__)
app.secret_key = config_store.get_or_create_flask_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)


def _auth_required() -> bool:
    """Login is enforced only when the admin opted in AND an admin account
    exists. The has_admin() guard prevents ever locking everyone out (e.g. if
    login is toggled on before any account is created)."""
    return is_login_required() and has_admin()


@app.context_processor
def inject_app_version():
    """Expose version and setup/auth state to every template."""
    return {
        "app_version": APP_VERSION,
        "authenticated": bool(session.get("authenticated")),
        "setup_complete": is_setup_complete(),
        "auth_required": _auth_required(),
    }


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if _auth_required() and not session.get("authenticated"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@app.before_request
def _gate_requests():
    """Refresh live config and enforce setup/auth gating on every request."""
    fm.load_runtime_config()
    endpoint = request.endpoint or ""
    if endpoint in PUBLIC_ENDPOINTS:
        return None
    # First-run onboarding: everything funnels to /setup until it is complete.
    if not is_setup_complete():
        if endpoint in SETUP_ENDPOINTS:
            return None
        return redirect(url_for("setup"))
    # After setup, require authentication only if the admin opted in.
    if _auth_required() and not session.get("authenticated"):
        if endpoint == "login":
            return None
        return redirect(url_for("login", next=request.path))
    return None

# Trusted proxy configuration
TRUSTED_PROXIES = os.getenv("TRUSTED_PROXIES", "")
REAL_IP_HEADER = os.getenv("REAL_IP_HEADER", "X-Forwarded-For")


def _is_trusted_proxy(ip: str) -> bool:
    """Check if IP is in the trusted proxy list (supports IPs and CIDRs)."""
    if not TRUSTED_PROXIES or not ip:
        return False
    try:
        client_ip = ipaddress.ip_address(ip)
        for trusted in TRUSTED_PROXIES.split(","):
            trusted = trusted.strip()
            if not trusted:
                continue
            if "/" in trusted:
                if client_ip in ipaddress.ip_network(trusted, strict=False):
                    return True
            else:
                if client_ip == ipaddress.ip_address(trusted):
                    return True
    except ValueError:
        pass
    return False


def get_real_ip():
    """Get client IP, trusting forwarded headers only from trusted proxies."""
    remote = request.remote_addr or "0.0.0.0"
    if not _is_trusted_proxy(remote):
        return remote
    forwarded_for = request.headers.get(REAL_IP_HEADER)
    if forwarded_for:
        ip_candidate = forwarded_for.split(",")[0].strip()
        try:
            ipaddress.ip_address(ip_candidate)
            return ip_candidate
        except ValueError:
            pass
    return remote


limiter = Limiter(
    app=app,
    key_func=get_real_ip,
    default_limits=[],
    storage_uri=os.getenv("REDIS_URL", "memory://"),
    strategy="fixed-window",
)


def _audit_log(action: str, email: str, status: str, reason: str = None):
    ip_address = get_real_ip()
    method = request.method

    log_parts = [
        f"action={action}",
        f"email={email}",
        f"ip={ip_address}",
        f"method={method}",
        f"status={status}",
    ]
    if reason:
        log_parts.append(f"reason=\"{reason}\"")

    log_message = " ".join(log_parts)

    if status == "success":
        APP_LOGGER.info("AUDIT: %s", log_message)
    else:
        APP_LOGGER.warning("AUDIT: %s", log_message)


BASE_DIR = os.path.dirname(__file__)
FILES_DIR = os.path.join(BASE_DIR, "files")

AVAILABLE_LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
SEERR_DIAGNOSTIC_REDACT_KEYS = {
    "apiKey",
    "displayName",
    "email",
    "password",
    "plexToken",
    "plexUsername",
    "token",
}

@app.route("/assets/<path:filename>")
def asset(filename: str):
    return send_from_directory(FILES_DIR, filename)


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(FILES_DIR, "favicon.png")


@app.after_request
def add_no_cache_headers(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def _request_wants_json() -> bool:
    """Determine whether the current request expects a JSON response."""
    if request.headers.get("X-Requested-With", "").lower() == "fetch":
        return True
    best = request.accept_mimetypes.best
    return best == "application/json"


def _json_or_flash(payload: dict, redirect_endpoint: str = "settings"):
    if _request_wants_json():
        status = int(payload.pop("_status", 200))
        return jsonify(payload), status
    category = payload.get("category") or ("success" if payload.get("ok") else "error")
    flash(payload.get("message") or "Action started.", category)
    return redirect(url_for(redirect_endpoint))


def _start_background_task(name: str, task_func) -> dict:
    task_id = uuid.uuid4().hex
    BACKGROUND_TASKS[task_id] = {
        "id": task_id,
        "name": name,
        "status": "running",
        "message": f"{name} is running. View the logs for details.",
        "category": "info",
    }

    def _target():
        try:
            message = task_func()
            BACKGROUND_TASKS[task_id].update(
                {
                    "status": "complete",
                    "message": message or f"{name} complete.",
                    "category": "success",
                }
            )
        except Exception as exc:
            APP_LOGGER.exception("%s failed: %s", name, exc)
            BACKGROUND_TASKS[task_id].update(
                {
                    "status": "failed",
                    "message": f"{name} failed: {exc}",
                    "category": "error",
                }
            )

    Thread(
        target=_target,
        name=f"forgotten-movies-task-{task_id}",
        daemon=True,
    ).start()
    return {
        "ok": True,
        "task_id": task_id,
        "status": "running",
        "category": "info",
        "message": f"{name} is running. View the logs for details.",
    }


def trigger_job(reason: str, async_run: bool) -> tuple[bool, str]:
    try:
        lock = acquire_job_lock(timeout=0.0)
    except Timeout:
        APP_LOGGER.info("Job already running; skipping %s trigger.", reason)
        return False, "Job is already running."

    def _target(acquired_lock):
        try:
            execute_job(reason)
        finally:
            acquired_lock.release()

    if async_run:
        Thread(
            target=_target,
            args=(lock,),
            name=f"forgotten-movies-job-{reason}",
            daemon=True,
        ).start()
        return True, "Job started."

    _target(lock)
    return True, "Job started."


def _run_with_job_lock(action_name: str, callback) -> str:
    try:
        lock = acquire_job_lock(timeout=0.0)
    except Timeout:
        APP_LOGGER.info("%s skipped; another job is already running.", action_name)
        return "Another job is already running. View the logs for details."
    try:
        return callback()
    finally:
        lock.release()


def _format_unsubscribe_records(records):
    formatted = []
    for record in records:
        raw = record.get("unsubscribed_at")
        display = None
        if raw:
            try:
                display = datetime.fromisoformat(raw).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                display = raw
        entry = dict(record)
        entry["unsubscribed_display"] = display
        formatted.append(entry)
    return formatted


def _redact_payload(value):
    if isinstance(value, dict):
        redacted = {}
        for key, child in value.items():
            if key in SEERR_DIAGNOSTIC_REDACT_KEYS:
                redacted[key] = "<redacted>"
            else:
                redacted[key] = _redact_payload(child)
        return redacted
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    return value


def _redirect_after_request_action(default_anchor: str):
    if request.form.get("next") == "stats":
        user = (request.form.get("user") or "").strip()
        if user:
            return redirect(url_for("stats", user=user))
        return redirect(url_for("stats"))
    return redirect(url_for("index", _anchor=default_anchor))


def _stats_filter_kwargs() -> dict[str, str | None]:
    return {
        "date_range": request.args.get("date_range"),
        "start_date": request.args.get("start_date"),
        "end_date": request.args.get("end_date"),
    }


@app.route("/", methods=["GET"])
def index():
    todo_items = get_overdue_requests_for_ui()
    stats_summary = get_user_stats(**_stats_filter_kwargs())
    todo_messages: list[tuple[str, str]] = []
    recent_emails = get_recent_sent_emails()
    recent_messages: list[tuple[str, str]] = []
    unsubscribe_records = _format_unsubscribe_records(list_unsubscribed_emails())
    unsubscribe_messages: list[tuple[str, str]] = []
    messages = []

    for category, text in get_flashed_messages(with_categories=True):
        if category.startswith("unsubscribe-"):
            unsubscribe_messages.append((category, text))
        elif category.startswith("todo-"):
            todo_messages.append((category, text))
        elif category.startswith("recent-"):
            recent_messages.append((category, text))
        else:
            messages.append((category, text))
    return render_template(
        "dashboard.html",
        page_title="Dashboard",
        messages=messages,
        todo_messages=todo_messages,
        recent_messages=recent_messages,
        unsubscribe_messages=unsubscribe_messages,
        todo_items=todo_items,
        stats_summary=stats_summary,
        recent_emails=recent_emails,
        unsubscribe_records=unsubscribe_records,
        scheduler_disabled=is_scheduler_disabled(),
        current_year=time.strftime("%Y"),
    )


@app.route("/stats", methods=["GET"])
def stats():
    stats_data = get_user_stats(request.args.get("user"), **_stats_filter_kwargs())
    return render_template(
        "stats.html",
        page_title="Overall Stats",
        messages=get_flashed_messages(with_categories=True),
        stats=stats_data,
        current_year=time.strftime("%Y"),
    )


@app.route("/requests/<request_id>/skip", methods=["POST"])
def skip_request(request_id):
    identifier = str(request_id)
    predicate = Request.id.test(lambda value: str(value) == identifier)
    record = request_db.get(predicate)
    if not record:
        flash("Unable to find that request.", "todo-error")
        return _redirect_after_request_action("todo-card")
    if record.get("skip_email"):
        flash("Reminders are already disabled for this item.", "todo-info")
        return _redirect_after_request_action("todo-card")
    request_db.update({"skip_email": True}, predicate)
    title = record.get("title") or "this request"
    APP_LOGGER.info("Manual action: marked request %s (%s) as do-not-send", identifier, title)
    flash(f"Won't send reminders for {title}.", "todo-success")
    return _redirect_after_request_action("todo-card")


@app.route("/requests/<request_id>/send", methods=["POST"])
def send_request_now(request_id):
    identifier = str(request_id)
    predicate = Request.id.test(lambda value: str(value) == identifier)
    record = request_db.get(predicate)
    if not record:
        flash("Unable to find that request.", "todo-error")
        return _redirect_after_request_action("todo-card")

    email_value = (record.get("email") or "").strip()
    if not email_value:
        flash("Request is missing an email address.", "todo-error")
        return _redirect_after_request_action("todo-card")

    now = datetime.now()
    user_record = get_email_user(email_value)
    APP_LOGGER.info("Manual action: send-now requested for %s (request %s)", email_value, identifier)

    try:
        outcome = _attempt_send_request(
            record,
            {},
            user_record=user_record,
            respect_cycle=False,
            respect_cooldown=False,
            perform_db_updates=not fm.DEBUG_MODE,
            allow_sleep=False,
            now_dt=now,
        )
    except Exception as exc:
        APP_LOGGER.exception("Failed to send email for request %s: %s", identifier, exc)
        flash(f"Failed to send email: {str(exc)}", "todo-error")
        return redirect(url_for("index", _anchor="todo-card"))

    if not outcome.sent:
        APP_LOGGER.info("Send-now skipped for request %s: %s", identifier, outcome.message)
        flash(outcome.message or "Reminder not sent.", "todo-info")
        return _redirect_after_request_action("todo-card")

    if fm.DEBUG_MODE:
        flash(f"[Debug] {outcome.message} (not persisted).", "todo-info")
    else:
        flash(outcome.message, "todo-success")
    APP_LOGGER.info("Send-now completed for request %s", identifier)
    return _redirect_after_request_action("todo-card")

@app.route("/unsubscribe", methods=["POST"])
def unsubscribe_email():
    email = (request.form.get("email") or "").strip().lower()
    wants_json = _request_wants_json()
    if not email:
        message = "Please provide an email address."
        if wants_json:
            return jsonify({"success": False, "message": message})
        flash(message, "unsubscribe-error")
        return redirect(url_for("index"))
    add_unsubscribed_email(email)
    _audit_log(action="manual_unsubscribe", email=email, status="success")
    updated_record = get_email_user(email)
    formatted = (
        _format_unsubscribe_records([dict(updated_record)]) if updated_record else []
    )
    payload = formatted[0] if formatted else {"email": email, "unsubscribed_at": None, "unsubscribed_display": None}
    message = f"Added {payload['email']} to the unsubscribe list."
    if wants_json:
        return jsonify(
            {
                "success": True,
                "message": message,
                "email": payload.get("email", email),
                "unsubscribed_at": payload.get("unsubscribed_at"),
                "unsubscribed_display": payload.get("unsubscribed_display"),
                "remove_url": url_for("remove_email"),
            }
        )
    flash(message, "unsubscribe-success")
    return redirect(url_for("index"))


@app.route("/unsubscribe/remove", methods=["POST"])
def remove_email():
    email = (request.form.get("email") or "").strip().lower()
    wants_json = _request_wants_json()
    if not email:
        message = "Email address missing for removal."
        if wants_json:
            return jsonify({"success": False, "message": message})
        flash(message, "unsubscribe-error")
        return redirect(url_for("index"))
    removed = remove_unsubscribed_email(email)
    if removed:
        _audit_log(action="remove_from_unsubscribe_list", email=email, status="success")
        message = f"Removed {email} from the unsubscribe list."
        if wants_json:
            return jsonify({"success": True, "message": message, "email": email})
        flash(message, "unsubscribe-success")
    else:
        _audit_log(
            action="remove_from_unsubscribe_list",
            email=email,
            status="not_found",
            reason="Email was not in unsubscribe list"
        )
        message = f"{email} was not on the unsubscribe list."
        if wants_json:
            return jsonify({"success": False, "message": message, "email": email})
        flash(message, "unsubscribe-info")
    return redirect(url_for("index"))


@app.route("/unsubscribe/<token>", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def unsubscribe_via_link(token: str):
    if not fm.UNSUBSCRIBE_ENABLED:
        APP_LOGGER.warning("Unsubscribe endpoint accessed but feature is disabled")
        if request.method == "POST":
            return "Feature disabled", 404
        return render_template(
            "unsubscribe_result.html",
            page_title="Feature Disabled",
            success=False,
            email=None,
            message="Self-service unsubscribe is not enabled. Please contact the administrator.",
            current_year=time.strftime("%Y"),
        ), 404

    try:
        decoded_email = _decrypt_email(token).strip().lower()
    except Exception as exc:
        _audit_log(
            action="unsubscribe_attempt",
            email="<invalid_token>",
            status="failure",
            reason=f"Token decrypt failed: {exc}"
        )
        if request.method == "POST":
            return "Invalid or expired token", 400
        return render_template(
            "unsubscribe_result.html",
            page_title="Invalid or Expired Link",
            success=False,
            email=None,
            message="This unsubscribe link is invalid or has expired. Links expire after 90 days.",
            current_year=time.strftime("%Y"),
        ), 400

    if request.method == "GET":
        return render_template(
            "unsubscribe_confirm.html",
            token=token,
            email=decoded_email,
            current_year=time.strftime("%Y"),
        )

    is_one_click = request.form.get('List-Unsubscribe') == 'One-Click'

    add_unsubscribed_email(decoded_email)
    method_type = "one-click" if is_one_click else "web_form"
    _audit_log(action=f"unsubscribe_{method_type}", email=decoded_email, status="success")

    # RFC 8058 one-click: minimal response for email client compatibility
    if is_one_click:
        return "", 200

    try:
        resubscribe_url = build_resubscribe_url(decoded_email)
    except Exception as exc:
        APP_LOGGER.warning("Failed to generate resubscribe URL: %s", exc)
        resubscribe_url = None

    return render_template(
        "unsubscribe_result.html",
        page_title="Unsubscribed Successfully",
        success=True,
        email=decoded_email,
        message="You have been unsubscribed from Forgotten Movies reminders.",
        resubscribe_url=resubscribe_url,
        current_year=time.strftime("%Y"),
    )


@app.route("/resubscribe/<token>", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def resubscribe_via_link(token: str):
    if not fm.UNSUBSCRIBE_ENABLED:
        APP_LOGGER.warning("Resubscribe endpoint accessed but feature is disabled")
        if request.method == "POST":
            return "Feature disabled", 404
        return render_template(
            "resubscribe_result.html",
            page_title="Feature Disabled",
            success=False,
            email=None,
            message="Self-service unsubscribe is not enabled. Please contact the administrator.",
            current_year=time.strftime("%Y"),
        ), 404

    try:
        decoded_email = _decrypt_email(token).strip().lower()
    except Exception as exc:
        _audit_log(
            action="resubscribe_attempt",
            email="<invalid_token>",
            status="failure",
            reason=f"Token decrypt failed: {exc}"
        )
        if request.method == "POST":
            return "Invalid or expired token", 400
        return render_template(
            "resubscribe_result.html",
            page_title="Invalid or Expired Link",
            success=False,
            email=None,
            message="This resubscribe link is invalid or has expired. Links expire after 90 days.",
            current_year=time.strftime("%Y"),
        ), 400

    if request.method == "GET":
        return render_template(
            "resubscribe_confirm.html",
            token=token,
            email=decoded_email,
            current_year=time.strftime("%Y"),
        )

    was_unsubscribed = remove_unsubscribed_email(decoded_email)

    if not was_unsubscribed:
        _audit_log(
            action="resubscribe",
            email=decoded_email,
            status="already_subscribed",
            reason="Email was not in unsubscribe list"
        )
        return render_template(
            "resubscribe_result.html",
            page_title="Already Subscribed",
            success=True,
            email=decoded_email,
            message="You are already subscribed to Forgotten Movies reminders.",
            current_year=time.strftime("%Y"),
        )

    _audit_log(action="resubscribe", email=decoded_email, status="success")

    return render_template(
        "resubscribe_result.html",
        page_title="Resubscribed Successfully",
        success=True,
        email=decoded_email,
        message="You have been resubscribed to Forgotten Movies reminders.",
        current_year=time.strftime("%Y"),
    )


@app.route("/run-now", methods=["POST"])
def run_now():
    success, msg = trigger_job("manual", async_run=True)
    payload = {
        "ok": success,
        "status": "running" if success else "busy",
        "category": "info" if success else "warning",
        "message": "Job started. View the logs for details." if success else msg,
    }
    if _request_wants_json():
        return jsonify(payload), 202 if success else 409
    flash(payload["message"], payload["category"])
    if request.form.get("next") == "settings":
        return redirect(url_for("settings"))
    return redirect(url_for("index"))


@app.route("/settings/task-status/<task_id>", methods=["GET"])
def settings_task_status(task_id: str):
    task = BACKGROUND_TASKS.get(task_id)
    if not task:
        return jsonify({"ok": False, "status": "unknown", "message": "Task status is no longer available."}), 404
    return jsonify({"ok": task["status"] != "failed", **task})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "version": APP_VERSION}), 200


@app.route("/logs", methods=["GET"])
def view_logs():
    flush_log_handlers()
    log_text = "Log file not found."
    try:
        with open(LOG_FILE_PATH, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
            log_text = "".join(lines[-500:]) if lines else "Log file is empty."
    except FileNotFoundError:
        pass
    return render_template(
        "logs.html",
        page_title="Logs",
        log_text=log_text,
        messages=get_flashed_messages(with_categories=True),
        available_levels=AVAILABLE_LOG_LEVELS,
        current_level=get_log_level(),
        current_year=time.strftime("%Y"),
    )


@app.route("/logs/level", methods=["POST"])
def update_log_level():
    level = (request.form.get("level") or "").upper()
    if set_log_level(level):
        flash(f"Log level set to {level}.", "success")
    else:
        flash(f"Invalid log level: {level}.", "error")
    return redirect(url_for("view_logs"))


@app.route("/logs/clear", methods=["POST"])
def clear_logs():
    for suffix in ("", ".1", ".2", ".3", ".4", ".5"):
        path = f"{LOG_FILE_PATH}{suffix}"
        try:
            with open(path, "w", encoding="utf-8"):
                pass
        except FileNotFoundError:
            continue
    flush_log_handlers()
    flash("Cleared log files.", "success")
    return redirect(url_for("view_logs"))


@app.route("/logs/data", methods=["GET"])
def logs_data():
    flush_log_handlers()
    log_text = ""
    try:
        with open(LOG_FILE_PATH, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
            log_text = "".join(lines[-500:]) if lines else ""
    except FileNotFoundError:
        pass
    return jsonify({"log": log_text})


# ---------------------------------------------------------------------------
# Configuration form helpers (shared by the setup wizard and Settings page)
# ---------------------------------------------------------------------------
def _config_field_view(field: dict, use_env_prefill: bool = False) -> dict:
    key = field["key"]
    is_secret = field["type"] == "secret"
    is_set = config_store.is_configured(key)
    stored = config_store.get(key)  # effective value, includes defaults

    prefill = env_prefill(key) if (use_env_prefill and not is_set) else ""

    if is_secret:
        value = ""  # never echo secrets back to the browser
    elif prefill:
        value = prefill
    else:
        value = "" if stored is None else stored

    if field["type"] == "bool":
        if is_set:
            checked = bool(stored)
        elif prefill:
            checked = str(prefill).strip().lower() in {"1", "true", "on", "yes"}
        else:
            checked = bool(stored)
    else:
        checked = False

    return {
        "key": key,
        "label": field["label"],
        "help": field.get("help", ""),
        "type": field["type"],
        "choices": field.get("choices", []),
        "required": field.get("required", False),
        "value": value,
        "checked": checked,
        "is_set": is_set,
    }


def _config_sections(use_env_prefill: bool = False) -> list[dict]:
    sections = []
    for name in SECTIONS:
        fields = [
            _config_field_view(f, use_env_prefill)
            for f in CONFIG_SCHEMA
            if f["section"] == name
        ]
        sections.append({"name": name, "fields": fields})
    return sections


def _all_present_keys() -> str:
    return ",".join(f["key"] for f in CONFIG_SCHEMA)


def _collect_submitted() -> dict:
    """Collect submitted config values, scoped to the keys the form declared."""
    valid = {f["key"]: f for f in CONFIG_SCHEMA}
    present = [
        k.strip()
        for k in (request.form.get("present_keys") or "").split(",")
        if k.strip() in valid
    ]
    out: dict = {}
    for key in present:
        if valid[key]["type"] == "bool":
            out[key] = "on" if request.form.get(key) else ""
        else:
            out[key] = request.form.get(key, "")
    return out


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
@limiter.limit("15 per minute")
def login():
    if not is_setup_complete():
        return redirect(url_for("setup"))
    if not _auth_required():
        # Login is disabled (or no admin account exists) — nothing to log in to.
        return redirect(url_for("index"))
    if session.get("authenticated"):
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if verify_admin(username, password):
            session["authenticated"] = True
            APP_LOGGER.info("Admin login succeeded for username=%s", username)
            nxt = request.args.get("next") or request.form.get("next") or ""
            if not (nxt.startswith("/") and not nxt.startswith("//")):
                nxt = url_for("index")
            return redirect(nxt)
        APP_LOGGER.warning("Failed login attempt for username=%s from %s", username, get_real_ip())
        flash("Invalid username or password.", "error")
    return render_template(
        "login.html",
        page_title="Sign In",
        next=request.args.get("next", ""),
        messages=get_flashed_messages(with_categories=True),
        current_year=time.strftime("%Y"),
    )


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    flash("Signed out.", "success")
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# First-run onboarding wizard
# ---------------------------------------------------------------------------
@app.route("/setup", methods=["GET", "POST"])
def setup():
    if is_setup_complete():
        return redirect(url_for("index") if session.get("authenticated") else url_for("login"))

    if request.method == "POST":
        require_login = request.form.get("require_login") == "on"
        username = (request.form.get("admin_username") or "").strip()
        password = request.form.get("admin_password") or ""
        confirm = request.form.get("admin_password_confirm") or ""

        errors: list[str] = []
        # An admin account is mandatory only when login is required; otherwise
        # it is optional (but still validated if the user chose to fill it in).
        wants_admin = require_login or username or password
        if wants_admin:
            if not username or not password:
                errors.append("An admin username and password are required to enable login.")
            elif password != confirm:
                errors.append("Passwords do not match.")
            elif len(password) < 8:
                errors.append("Password must be at least 8 characters.")

        errors.extend(save_settings(_collect_submitted()))
        fm.load_runtime_config()

        missing = validate_required()
        if missing:
            errors.append("Please fill in the required fields: " + ", ".join(missing))

        if errors:
            for err in errors:
                flash(err, "error")
            return redirect(url_for("setup"))

        if username and password:
            set_admin(username, password)
        set_login_required(require_login)
        mark_setup_complete(True)
        fm.load_runtime_config()
        session["authenticated"] = True
        APP_LOGGER.info(
            "First-time setup completed (login %s; admin account %s).",
            "required" if require_login else "disabled",
            "created" if (username and password) else "not set",
        )
        flash("Setup complete. Welcome to Forgotten Movies!", "success")
        return redirect(url_for("index"))

    return render_template(
        "setup.html",
        page_title="Welcome",
        sections=_config_sections(use_env_prefill=True),
        present_keys=_all_present_keys(),
        messages=get_flashed_messages(with_categories=True),
        current_year=time.strftime("%Y"),
    )


@app.route("/settings/test-connection", methods=["POST"])
@app.route("/setup/test-connection", methods=["POST"])
def test_connection():
    service = (request.form.get("service") or "").lower()
    if service == "seerr":
        url = request.form.get("OVERSEERR_URL")
        key = request.form.get("OVERSEERR_API_KEY") or config_store.get("OVERSEERR_API_KEY")
        ok, message = test_overseerr_connection(url, key)
    elif service == "tautulli":
        url = request.form.get("TAUTULLI_URL")
        key = request.form.get("TAUTULLI_API_KEY") or config_store.get("TAUTULLI_API_KEY")
        ok, message = test_tautulli_connection(url, key)
    else:
        ok, message = False, "Unknown service."
    return jsonify({"ok": ok, "success": ok, "message": message}), (200 if ok else 400)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        disabled = request.form.get("scheduler_disabled") == "on"
        set_scheduler_disabled(disabled)
        msg = "Scheduler disabled. Automated scans are paused." if disabled else "Scheduler enabled. Automated scans resumed."
        flash(msg, "success")
        return redirect(url_for("settings"))

    return render_template(
        "settings.html",
        page_title="Settings",
        sections=_config_sections(),
        present_keys=_all_present_keys(),
        scheduler_disabled=is_scheduler_disabled(),
        job_status=get_job_status(),
        using_custom_template=is_using_custom_email_template(),
        login_required_setting=is_login_required(),
        has_admin_account=has_admin(),
        admin_username=config_store.get_admin_username() or "",
        messages=get_flashed_messages(with_categories=True),
        current_year=time.strftime("%Y"),
    )


@app.route("/settings/save", methods=["POST"])
def save_config():
    errors = save_settings(_collect_submitted())
    fm.load_runtime_config()
    if errors:
        for err in errors:
            flash(err, "error")
    else:
        flash("Settings saved.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/account", methods=["POST"])
def update_account():
    """Update the require-login toggle and optionally the admin credentials."""
    require_login = request.form.get("require_login") == "on"
    username = (request.form.get("admin_username") or "").strip()
    password = request.form.get("admin_password") or ""
    confirm = request.form.get("admin_password_confirm") or ""

    errors: list[str] = []
    # Only treat this as a credential change when a new password was entered
    # (the username field is pre-filled, so it alone shouldn't count).
    changing_password = bool(password or confirm)
    if changing_password:
        if not username:
            errors.append("A username is required to set the admin password.")
        elif password != confirm:
            errors.append("Passwords do not match.")
        elif len(password) < 8:
            errors.append("Password must be at least 8 characters.")

    # Prevent locking everyone out: can't require login without an admin account.
    if require_login and not has_admin() and not changing_password:
        errors.append("Set an admin username and password before requiring login.")

    if errors:
        for err in errors:
            flash(err, "error")
        return redirect(url_for("settings"))

    if changing_password:
        set_admin(username, password)
    set_login_required(require_login)
    fm.load_runtime_config()
    APP_LOGGER.info("Account settings updated (login %s).", "required" if require_login else "disabled")
    flash("Account settings updated.", "success")
    return redirect(url_for("settings"))


# ---------------------------------------------------------------------------
# Email template editor
# ---------------------------------------------------------------------------
@app.route("/settings/email-template", methods=["GET"])
def email_template_editor():
    return render_template(
        "email_template_editor.html",
        page_title="Email Template",
        template_source=get_email_template_source(),
        default_source=get_default_email_template_source(),
        variables=EMAIL_TEMPLATE_VARIABLES,
        using_custom=is_using_custom_email_template(),
        messages=get_flashed_messages(with_categories=True),
        current_year=time.strftime("%Y"),
    )


@app.route("/settings/email-template/save", methods=["POST"])
def save_email_template():
    source = request.form.get("template_source", "")
    try:
        save_email_template_source(source)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("email_template_editor"))
    flash("Email template saved.", "success")
    return redirect(url_for("email_template_editor"))


@app.route("/settings/email-template/reset", methods=["POST"])
def reset_email_template_route():
    reset_email_template()
    flash("Reverted to the default email template.", "success")
    return redirect(url_for("email_template_editor"))


@app.route("/settings/email-template/preview", methods=["POST"])
def preview_email_template():
    source = request.form.get("template_source") or None
    try:
        html = render_email_preview(source)
        return jsonify({"ok": True, "html": html})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400


@app.route("/settings/update-watch-status", methods=["POST"])
def update_watch_status():
    APP_LOGGER.info("Manual action: update watch status requested")
    def _task():
        stats = check_unwatched_emails_status()
        return (
            f"Watch status check complete: {stats['checked']} requester checks, "
            f"{stats['watched']} requester watched, {stats.get('media_checked', 0)} media checks, "
            f"{stats.get('media_watched', 0)} watched by anyone, {stats['failed']} failed."
        )

    payload = _start_background_task(
        "Watch status check",
        lambda: _run_with_job_lock("Watch status check", _task),
    )
    return _json_or_flash(payload)


@app.route("/settings/refresh-unknown-metadata", methods=["POST"])
def refresh_unknown_metadata():
    APP_LOGGER.info("Manual action: recent unknown metadata refresh requested")
    def _task():
        limit = fm.TAUTULLI_RECENT_UNKNOWN_METADATA_LIMIT
        updates = refresh_metadata_for_recent_unknowns(
            limit=limit,
            pool_size=max(50, limit),
        )
        return f"Recent unknown metadata refresh complete: {len(updates)} title(s) updated."

    payload = _start_background_task(
        "Recent unknown metadata refresh",
        lambda: _run_with_job_lock("Recent unknown metadata refresh", _task),
    )
    return _json_or_flash(payload)


def _test_result(ok: bool, message: str, redirect_endpoint: str = "settings"):
    """Return a JSON result for fetch callers, or flash + redirect otherwise."""
    if _request_wants_json():
        return jsonify({"ok": ok, "success": ok, "message": message}), (200 if ok else 400)
    flash(message, "success" if ok else "error")
    return redirect(url_for(redirect_endpoint))


@app.route("/settings/test-email", methods=["POST"])
def send_test_email():
    # If email-section fields were submitted (settings page or setup wizard),
    # persist them first so the test uses exactly what the user entered. A blank
    # secret field (masked password) is preserved by save_settings.
    email_keys = {f["key"] for f in CONFIG_SCHEMA if f["section"] == "Email"}
    submitted = {k: request.form.get(k) for k in email_keys if k in request.form}
    if submitted:
        errors = save_settings(submitted)
        fm.load_runtime_config()
        if errors:
            return _test_result(False, "; ".join(errors))

    recipient = (request.form.get("test_email") or "").strip() or (fm.FROM_EMAIL_ADDRESS or "").strip()
    if not recipient:
        return _test_result(False, "Enter a destination address or set a From address first.")

    APP_LOGGER.info("Manual action: test email requested for %s", recipient)
    subject = "Forgotten Movies test email"
    body = (
        "<p>This is a test email from Forgotten Movies.</p>"
        "<p>If you received this, your SMTP settings are working correctly.</p>"
        f"<p style=\"color:#888; font-size:0.85em;\">Sent from Forgotten Movies v{APP_VERSION}.</p>"
    )
    try:
        actual_recipient = send_email(recipient, subject, body, is_html=True)
    except Exception as exc:
        APP_LOGGER.exception("Test email failed: %s", exc)
        return _test_result(False, f"Test email failed: {exc}")

    if fm.DEBUG_MODE and actual_recipient != recipient:
        return _test_result(True, f"Debug mode is on: test email redirected to {actual_recipient}.")
    return _test_result(True, f"Test email sent to {actual_recipient}.")


@app.route("/settings/log-seerr-payload", methods=["POST"])
def log_seerr_payload():
    APP_LOGGER.info("Manual diagnostic: Seerr request payload logging requested")
    try:
        requests_payload = get_overseerr_requests()
        sample = requests_payload[:5]
        redacted_sample = _redact_payload(sample)
        APP_LOGGER.info(
            "SEERR REQUEST PAYLOAD DIAGNOSTIC START count=%s sample_count=%s",
            len(requests_payload),
            len(sample),
        )
        APP_LOGGER.info(
            "SEERR REQUEST PAYLOAD DIAGNOSTIC JSON:\n%s",
            json.dumps(redacted_sample, indent=2, sort_keys=True, default=str),
        )
        APP_LOGGER.info("SEERR REQUEST PAYLOAD DIAGNOSTIC END")
        flash(
            f"Logged {len(sample)} redacted Seerr request payload(s). Open Logs to inspect them.",
            "success",
        )
    except Exception as exc:
        APP_LOGGER.exception("Failed to log Seerr request payload: %s", exc)
        flash(f"Failed to log Seerr request payload: {exc}", "error")
    return redirect(url_for("settings"))


@app.route("/settings/log-seerr-payload-window", methods=["POST"])
def log_seerr_payload_window():
    range_value = (request.form.get("request_range") or "").strip()
    try:
        start_text, end_text = range_value.split("-", 1)
        start = int(start_text.strip())
        end = int(end_text.strip())
        if start < 0 or end < start:
            raise ValueError
    except ValueError:
        flash("Enter a valid Seerr request range, like 150-160.", "error")
        return redirect(url_for("settings"))

    skip = start
    take = (end - start) + 1
    if take > 50:
        flash("Please log 50 Seerr requests or fewer at a time.", "error")
        return redirect(url_for("settings"))

    APP_LOGGER.info(
        "Manual diagnostic: Seerr request payload window logging requested skip=%s take=%s",
        skip,
        take,
    )
    try:
        requests_payload = get_overseerr_requests_page(skip=skip, take=take)
        redacted_payload = _redact_payload(requests_payload)
        APP_LOGGER.info(
            "SEERR REQUEST PAYLOAD WINDOW DIAGNOSTIC START skip=%s take=%s count=%s",
            skip,
            take,
            len(requests_payload),
        )
        APP_LOGGER.info(
            "SEERR REQUEST PAYLOAD WINDOW DIAGNOSTIC JSON:\n%s",
            json.dumps(redacted_payload, indent=2, sort_keys=True, default=str),
        )
        APP_LOGGER.info("SEERR REQUEST PAYLOAD WINDOW DIAGNOSTIC END")
        flash(
            f"Logged {len(requests_payload)} redacted Seerr request payload(s) from records {start}-{end}.",
            "success",
        )
    except Exception as exc:
        APP_LOGGER.exception("Failed to log Seerr request payload window: %s", exc)
        flash(f"Failed to log Seerr request payload window: {exc}", "error")
    return redirect(url_for("settings"))


@app.errorhandler(429)
def ratelimit_handler(e):
    APP_LOGGER.warning("Rate limit exceeded for %s: %s", get_real_ip(), request.path)

    # RFC 8058 one-click: minimal response for email client compatibility
    if request.method == "POST" and request.path.startswith("/unsubscribe"):
        return "Rate limit exceeded. Please try again later.", 429

    return render_template(
        "unsubscribe_result.html",
        page_title="Too Many Requests",
        success=False,
        email=None,
        message="You have made too many requests. Please wait a moment and try again.",
        current_year=time.strftime("%Y"),
    ), 429
