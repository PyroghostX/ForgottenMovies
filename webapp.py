import ipaddress
import logging
import os
import time
from datetime import datetime
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
    url_for,
)
from flask_limiter import Limiter

from forgotten_movies import (
    add_unsubscribed_email,
    check_unwatched_emails_status,
    flush_log_handlers,
    get_email_user,
    get_log_level,
    get_overdue_requests_for_ui,
    get_recent_sent_emails,
    is_scheduler_disabled,
    list_unsubscribed_emails,
    LOG_FILE_PATH,
    remove_unsubscribed_email,
    request_db,
    Request,
    set_scheduler_disabled,
    set_log_level,
    _attempt_send_request,
    DEBUG_MODE,
    UNSUBSCRIBE_ENABLED,
    _decrypt_email,
    build_resubscribe_url,
)
from filelock import Timeout

from job_runner import acquire_job_lock, execute_job

APP_LOGGER = logging.getLogger("ForgottenMoviesWeb")
APP_LOGGER.setLevel(logging.INFO)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-me")

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


@app.route("/", methods=["GET"])
def index():
    todo_items = get_overdue_requests_for_ui()
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
        recent_emails=recent_emails,
        unsubscribe_records=unsubscribe_records,
        scheduler_disabled=is_scheduler_disabled(),
        current_year=time.strftime("%Y"),
    )


@app.route("/requests/<request_id>/skip", methods=["POST"])
def skip_request(request_id):
    identifier = str(request_id)
    predicate = Request.id.test(lambda value: str(value) == identifier)
    record = request_db.get(predicate)
    if not record:
        flash("Unable to find that request.", "todo-error")
        return redirect(url_for("index", _anchor="todo-card"))
    if record.get("skip_email"):
        flash("Reminders are already disabled for this item.", "todo-info")
        return redirect(url_for("index", _anchor="todo-card"))
    request_db.update({"skip_email": True}, predicate)
    title = record.get("title") or "this request"
    APP_LOGGER.info("Manual action: marked request %s (%s) as do-not-send", identifier, title)
    flash(f"Won't send reminders for {title}.", "todo-success")
    return redirect(url_for("index", _anchor="todo-card"))


@app.route("/requests/<request_id>/send", methods=["POST"])
def send_request_now(request_id):
    identifier = str(request_id)
    predicate = Request.id.test(lambda value: str(value) == identifier)
    record = request_db.get(predicate)
    if not record:
        flash("Unable to find that request.", "todo-error")
        return redirect(url_for("index", _anchor="todo-card"))

    email_value = (record.get("email") or "").strip()
    if not email_value:
        flash("Request is missing an email address.", "todo-error")
        return redirect(url_for("index", _anchor="todo-card"))

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
            perform_db_updates=not DEBUG_MODE,
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
        return redirect(url_for("index", _anchor="todo-card"))

    if DEBUG_MODE:
        flash(f"[Debug] {outcome.message} (not persisted).", "todo-info")
    else:
        flash(outcome.message, "todo-success")
    APP_LOGGER.info("Send-now completed for request %s", identifier)
    return redirect(url_for("index", _anchor="todo-card"))

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
    if not UNSUBSCRIBE_ENABLED:
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
    if not UNSUBSCRIBE_ENABLED:
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
    flash(msg, "success" if success else "info")
    return redirect(url_for("index"))


@app.route("/health", methods=["GET"])
def health() -> tuple[str, int]:
    return "ok", 200


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
        scheduler_disabled=is_scheduler_disabled(),
        messages=get_flashed_messages(with_categories=True),
        current_year=time.strftime("%Y"),
    )


@app.route("/settings/update-watch-status", methods=["POST"])
def update_watch_status():
    APP_LOGGER.info("Manual action: update watch status requested")
    try:
        stats = check_unwatched_emails_status()
        msg = f"Watch status check complete: {stats['checked']} checked, {stats['watched']} watched, {stats['failed']} failed."
        flash(msg, "success")
    except Exception as exc:
        APP_LOGGER.exception("Watch status check failed: %s", exc)
        flash(f"Watch status check failed: {exc}", "error")
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
