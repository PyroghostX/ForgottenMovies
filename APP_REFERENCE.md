# Forgotten Movies App Reference

This note is a working map of the app so future changes can start from the right file quickly.

## Runtime Shape

- `forgotten_movies.py` is the core application module. It loads configuration from environment variables, initializes TinyDB stores in `/app/data`, talks to Seer/Tautulli/TMDB/SMTP, and contains the scheduled job logic.
- `webapp.py` is the Flask dashboard. It imports helpers and TinyDB handles from `forgotten_movies.py`, renders templates, and exposes manual actions.
- `scheduler_runner.py` starts the recurring job loop. It checks the scheduler-disabled setting before running.
- `job_runner.py` wraps `forgotten_movies.main()` with a file lock so manual and scheduled jobs do not overlap.
- `entrypoint.py` starts the web app and scheduler process in the Docker container.
- `templates/` contains the UI and email template.
- `files/` contains static assets such as logo, screenshots, and favicon files.

## Data Stores

All TinyDB files live in `DATA_DIR`, currently hardcoded as `/app/data`.

- `request_data.json` via `request_db`: one record per Seer request the app has seen.
- `email_data.json` via `email_db`: one record per reminder email that has been sent.
- `email_users.json` via `email_users_db`: per-email state such as cooldown and unsubscribe status.
- `settings.json` via `settings_db`: scheduler toggle and last watch-status check timestamp.

Important request fields:

- `id`: Seer request id.
- `mediaAddedDate`/`mediaAddedAt`/`createdAt`: used to decide when a request is old enough for reminders.
- `tmdbId`, `ratingkey`, `mediaType`: identifiers used for matching media and querying Tautulli.
- `plexUsername`, `email`: requester identity.
- `plexUrl`, `mobilePlexUrl`, `posterUrl`: reminder email assets/links.
- `tautulli_watch_date`: set when Tautulli says the requester already watched the item before a reminder is sent.
- `email_sent`, `skip_email`, `eligible_for_email`: reminder workflow flags.
- `title`: starts as `Unknown`, later filled from Tautulli metadata/history.
- New records try to store a real title immediately. The app first checks title-like fields from Seer, then asks Tautulli metadata for up to `TAUTULLI_NEW_REQUEST_METADATA_LIMIT` new unknown titles per run.

Important email fields:

- `rating_key`, `tmdbId`, `email`, `plex_username`, `title`, `mediaType`: the sent reminder target.
- `media_added_at`, `email_sent_at`: timeline fields for UI.
- `date_watched`: set by `check_unwatched_emails_status()` after Tautulli reports the user watched the reminded item.

## Core Job Flow

`forgotten_movies.main()` does the scheduled/manual work:

1. Checks Seer and Tautulli connectivity.
2. Runs `check_unwatched_emails_status()` at most once every 24 hours.
3. Pulls fulfilled requests from Seer with `get_overseerr_requests()`.
4. Inserts new request records into `request_db`.
5. Refreshes recent unknown titles using Tautulli via `refresh_metadata_for_recent_unknowns()`.
6. Groups overdue, unwatched, unsent requests by email.
7. Sends at most one reminder per user per run, respecting cooldowns.

## External APIs

- Seer: `GET {OVERSEERR_URL}/request?take=...&filter=available&sort=added`. The env var names remain `OVERSEERR_*` for backward compatibility.
- Tautulli:
  - `get_server_info` for startup checks.
  - `get_history` in `has_user_watched_media(user, rating_key, media_type)`.
  - `get_metadata` in `get_tautulli_metadata(rating_key)`.
- TMDB: optional poster lookup in `get_tmdb_poster()`.
- SMTP: reminder sending in `send_email()`.

## Web Routes

- `/`: dashboard with run-now, upcoming reminders, sent emails, and unsubscribe management.
- `/stats`: user stats summary and per-user drill-down based on local TinyDB data.
- `/requests/<request_id>/skip`: marks a request as do-not-send.
- `/requests/<request_id>/send`: sends a reminder immediately, bypassing cooldown/cycle checks.
- `/unsubscribe` and `/unsubscribe/remove`: manage email opt-outs.
- `/run-now`: starts the job asynchronously.
- `/logs`, `/logs/data`, `/logs/level`, `/logs/clear`: log viewer and controls.
- `/settings`: scheduler toggle.
- `/settings/update-watch-status`: manually checks sent reminders against Tautulli.
- `/health`: simple health check.

## Current Stats Notes

The app already has enough local data to show useful user stats:

- how many known available requests each user made,
- how many reminders were sent,
- how many reminded titles were later marked watched,
- which known titles are still not watched according to stored app state,
- which titles were skipped or already watched before reminder.

For fully authoritative stats, Tautulli is still needed. Local TinyDB data only updates watch status when the app has checked Tautulli during reminder evaluation or `check_unwatched_emails_status()`. A future "refresh all user stats" action should call Tautulli for every request with a requester, `ratingkey`, and `mediaType`, then store the result back onto request/email records.

## Current API Pull Volume

Per normal job run:

- Seer gets 1 request-list call: `GET /request?take={OVERSEERR_NUM_OF_HISTORY_RECORDS}&filter=available&sort=added`.
- Seer also gets 1 lightweight connectivity call before the run.
- Tautulli gets 1 lightweight connectivity call before the run.
- Tautulli gets up to `TAUTULLI_NEW_REQUEST_METADATA_LIMIT` metadata calls for brand-new Seer records whose title is still unknown. Default: 50 per run.
- Tautulli gets up to 1 history call per unwatched sent reminder during the 24-hour watch-status check.
- Tautulli checks up to 50 recent unknown request records, regardless of due date, but stops after refreshing 10 titles.
- Tautulli gets 1 history call for each overdue candidate evaluated for sending.
- Tautulli gets 1 metadata call only when a candidate title is still `Unknown`.

The stats page itself does not call Tautulli. That keeps page loads fast and avoids surprise API traffic, but it means stats are only as fresh as the locally stored watch state. Accurate all-request stats should be implemented as an explicit refresh action or scheduled background task, not as automatic work during `/stats` rendering.

## Good Extension Points

- Add cross-page UI in `templates/base.html`.
- Add new dashboard route handlers in `webapp.py`.
- Add reusable aggregation/query helpers in `forgotten_movies.py`.
- Reuse existing table sort/filter JavaScript in `templates/dashboard.html` if a new table needs full controls.
- Keep all long-running external checks behind explicit manual actions or the scheduler so page loads stay quick.
