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

from forgotten_movies import (
    add_unsubscribed_email,
    check_unwatched_emails_status,
    flush_log_handlers,
    get_email_user,
    get_log_level,
    get_overdue_requests_for_ui,
    get_recent_sent_emails,
    get_user_stats,
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
)
from filelock import Timeout

from job_runner import acquire_job_lock, execute_job

APP_LOGGER = logging.getLogger("ForgottenMoviesWeb")
APP_LOGGER.setLevel(logging.INFO)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-me")

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

    try:
        _target(lock)
    finally:
        pass
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


def _redirect_after_request_action(default_anchor: str):
    if request.form.get("next") == "stats":
        user = (request.form.get("user") or "").strip()
        if user:
            return redirect(url_for("stats", user=user))
        return redirect(url_for("stats"))
    return redirect(url_for("index", _anchor=default_anchor))


@app.route("/", methods=["GET"])
def index():
    todo_items = get_overdue_requests_for_ui()
    stats_summary = get_user_stats()
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
    stats_data = get_user_stats(request.args.get("user"))
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
    if record.get("skip_email"):
        request_db.update({"skip_email": False}, predicate)
        record = request_db.get(predicate) or record
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

    if not outcome.sent:
        APP_LOGGER.info("Send-now skipped for request %s: %s", identifier, outcome.message)
        flash(outcome.message or "Reminder not sent.", "todo-info")
        return _redirect_after_request_action("todo-card")

    if DEBUG_MODE:
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
    APP_LOGGER.info("Manual action: unsubscribing %s", email)
    add_unsubscribed_email(email)
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
        APP_LOGGER.info("Manual action: removed %s from unsubscribe list", email)
        message = f"Removed {email} from the unsubscribe list."
        if wants_json:
            return jsonify({"success": True, "message": message, "email": email})
        flash(message, "unsubscribe-success")
    else:
        APP_LOGGER.info("Manual action: attempted removal for %s but it was not listed", email)
        message = f"{email} was not on the unsubscribe list."
        if wants_json:
            return jsonify({"success": False, "message": message, "email": email})
        flash(message, "unsubscribe-info")
    return redirect(url_for("index"))


@app.route("/run-now", methods=["POST"])
def run_now():
    success, msg = trigger_job("manual", async_run=True)
    flash(msg, "success" if success else "info")
    if request.form.get("next") == "settings":
        return redirect(url_for("settings"))
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
