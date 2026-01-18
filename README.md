# <img src="files/logo.png" alt="Forgotten Movies logo" width="90"> Forgotten Movies 

Forgotten Movies keeps Plex requests from gathering dust. It watches Overseerr for requests that have been fulfilled, checks Tautulli to see whether the requester actually watched them, and sends a friendly email reminder when something has been available for too long.                 


# Features

- **Automated reminders:** Periodically scan Overseerr, cross-reference Tautulli history, and sends emails via SMTP to the original requester.
- **Custom email template:** Default ships to `/app/data/email_template_original.html`; add `/app/data/email_template.html` to override while still receiving upstream updates.
- **Dashboard:** Kick off manual runs, review the upcoming reminder queue, see recently sent reminders, and manage unsubscribed addresses.
- **Self-service unsubscribe (optional):** Let users unsubscribe themselves via encrypted links in emails instead of managing the list manually. Works with any reverse proxy setup.
- **Scheduler toggle:** Temporarily pause automated API calls and emails from the settings page.
- **Docker ready:** Single-container deployment with persistent TinyDB data, logs, and template files.


# Prerequisites

- Plex Media Server
- Tautulli
- Overseerr or Jellyseerr (Important: Make sure the "Web App url" set on your overseerr plex settings page)
- SMTP email so Forgotten Movies can send reminder emails (other methods built out later).
- TheMovieDB API key (optional but recommended) if you want poster artwork in the reminders.


# Preview

<p align="center">
  <img src="files/screenshot_email.png" alt="Screenshot Email" width="609">
</p>

<p align="center">
  <img src="files/screenshot_website.png" alt="Screenshot Website" width="788">
</p>

<p align="center">
  <img src="files/screenshot_website_2.png" alt="Screenshot Website" width="788">
</p>

# Environment Variables

| Key | Description |
|-----|-------------|
| `TAUTULLI_API_KEY`, `TAUTULLI_URL` | Tautulli credentials for watch history queries; the URL must include the v2 API endpoint (e.g. `https://tautulli.example.com/api/v2`). |
| `OVERSEERR_API_KEY`, `OVERSEERR_URL` | Overseerr or Jellyseerr API details used to pull fulfilled requests; point the URL at the `/api/v1` root (e.g. `https://request.example.com/api/v1`). |
| `THEMOVIEDB_API_KEY` | Fetches poster artwork for reminder emails. Leave unset to skip artwork (emails still send). |
| `SMTP_SERVER`, `SMTP_PORT` | SMTP host/port for STARTTLS email delivery (port defaults to 587). |
| `SMTP_ENCRYPTION` | One of `STARTTLS` (default), `SSL`, or `NONE`. If unset and `SMTP_PORT=465`, the app automatically picks `SSL`. |
| `SMTP_USERNAME` | Optional SMTP auth username when it differs from `FROM_EMAIL_ADDRESS`. |
| `FROM_EMAIL_ADDRESS`, `FROM_NAME`, `EMAIL_PASSWORD` | Outbound email identity and password. |
| `BCC_EMAIL_ADDRESS` | Optional address copied on reminders (you may also set it equal to `FROM_EMAIL_ADDRESS`). |
| `ADMIN_NAME` | Shown in reminder copy so recipients know who to contact. |
| `OVERSEERR_NUM_OF_HISTORY_RECORDS` | Number of Overseerr entries fetched per scan (default 10). |
| `DAYS_SINCE_REQUEST`, `DAYS_SINCE_REQUEST_EMAIL_TEXT` | Minimum days since request fulfillment before a reminder is sent (default 90) and the human-readable text used in the email template (default `"3 months"`). |
| `HOURS_BETWEEN_EMAILS` | Cooldown window per recipient (default 24 hours). |
| `REQUEST_URL` | Link back to your request portal (used in the email footer). |
| `JOB_INTERVAL_SECONDS` | Scheduler frequency (default 600 seconds). |
| `INITIAL_DELAY_SECONDS` | Startup delay before the first scheduled run (default 600 seconds). |
| `DISABLE_SCHEDULER` | Start with the scheduler disabled until re-enabled in the UI (default `false`). |
| `LOG_LEVEL` | One of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` (default `INFO`). |
| `LOG_FILE_MAX_BYTES`, `LOG_FILE_BACKUP_COUNT` | Rotating file handler settings (defaults: 1 MB, 3 backups). |
| `DEBUG_MODE` | When `true`, reroutes mail to `DEBUG_EMAIL`/`FROM_EMAIL_ADDRESS` and enforces `DEBUG_MAX_EMAILS` per run. |
| `DEBUG_EMAIL`, `DEBUG_MAX_EMAILS` | Override receiving address and cap while in debug mode (default max = 2). |
| `FLASK_SECRET_KEY` | Session/flash signing key for the Flask UI. |
| `EMAIL_TEMPLATE_PATH` | Optional custom override path for the HTML template (defaults to `/app/data/email_template.html`). |
| `UNSUBSCRIBE_SECRET_KEY` | Optional: Secret key for signing unsubscribe tokens. Generate with `python -c "import secrets; print(secrets.token_hex(32))"`. Leave unset to disable self-service unsubscribe. |
| `BASE_URL` | Optional: Public URL where your instance is accessible (e.g. `https://forgotten.example.com`). Required only when `UNSUBSCRIBE_SECRET_KEY` is set. |
| `TRUSTED_PROXIES` | Comma-separated IPs or CIDRs of trusted reverse proxies (e.g. `172.16.0.0/12,10.0.0.1`). Required to trust `REAL_IP_HEADER`. |
| `REAL_IP_HEADER` | Header containing client IP set by your reverse proxy (default: `X-Forwarded-For`). Only trusted when request comes from `TRUSTED_PROXIES`. Used for subscription management logging. |
| `REDIS_URL` | Optional Redis URL for rate limiting storage (e.g. `redis://localhost:6379/0`). Defaults to in-memory storage, which doesn't persist across restarts or scale across instances. |
| `JOB_LOCK_TIMEOUT` | Seconds to wait when acquiring the inter-process job lock (default `0.1`). |
| `ROOT`, `PUID`, `PGID`, `TZ` | Docker-only: bind mount root, container UID/GID, timezone. |

> **Tip:** When credentials contain characters such as `!`, `$`, `&`, or `#`, wrap the value in quotes (or store them inside an `.env` file) so your shell/YAML parser doesn't truncate or reinterpret the password.
> **Important:** The email template is mandatory. If neither `/app/data/email_template.html` nor `/app/data/email_template_original.html` can be read or formatted with the supplied placeholders, the job raises an error and no reminders are sent. This prevents accidents with blank messages.

# INSTALLATION

## Docker Compose Deployment

➡️ [`docker-compose.yml-example`](./docker-compose.yml-example)
```bash
Copy the contents of docker-compose.yml-example into your docker-compose.yml
# edit docker-compose.yml and .env with your API keys, SMTP credentials, and secrets
Make sure to update all the variables or this won't run correctly of course.
docker-compose up -d
open http://localhost:8741
```

## Docker run Deployment
```bash
docker run -d \
  --name forgotten-movies \
  --restart unless-stopped \
  -e PUID=${PUID} \
  -e PGID=${PGID} \
  -e TZ=${TZ} \
  -e TAUTULLI_URL="https://subdomain.example.com/api/v2" \
  -e TAUTULLI_API_KEY="YOUR_KEY" \
  -e OVERSEERR_URL="https://request.example.com/api/v1" \
  -e OVERSEERR_API_KEY="YOUR_KEY" \
  -e OVERSEERR_NUM_OF_HISTORY_RECORDS=200 \
  -e REQUEST_URL="https://request.example.com" \
  -e THEMOVIEDB_API_KEY="YOUR_KEY" \
  -e SMTP_SERVER="smtp.gmail.com" \
  -e SMTP_PORT=587 \
  -e SMTP_ENCRYPTION="STARTTLS" \
  -e SMTP_USERNAME="smtp-login@example.com" \
  -e FROM_NAME="Plex Forgotten Movies" \
  -e FROM_EMAIL_ADDRESS="email@gmail.com" \
  -e EMAIL_PASSWORD="password" \
  -e BCC_EMAIL_ADDRESS="email@gmail.com" \
  -e ADMIN_NAME="admin_name" \
  -e DAYS_SINCE_REQUEST=90 \
  -e DAYS_SINCE_REQUEST_EMAIL_TEXT="3 months" \
  -e HOURS_BETWEEN_EMAILS=168 \
  -e JOB_INTERVAL_SECONDS=3600 \
  -e INITIAL_DELAY_SECONDS=300 \
  -e FLASK_SECRET_KEY="YOUR_KEY" \
  -e LOG_LEVEL="INFO" \
  -e DEBUG_MODE="false" \
  -e DEBUG_MAX_EMAILS=2 \
  -e DEBUG_EMAIL="email@gmail.com" \
  -e DISABLE_SCHEDULER="false" \
  -p 8741:8741 \
  -v <your_data_path>:/app/data \
  pyroghostx/forgottenmovies:latest
```

## Unraid docker run delpoyment
```bash
docker run
  -d
  --name='forgotten-movies'
  --net='unraid'
  --pids-limit 2048
  -e TZ="Europe/Berlin"
  -e HOST_OS="Unraid"
  -e HOST_HOSTNAME="UNRAID-Server"
  -e HOST_CONTAINERNAME="forgotten-movies"
  -e 'PUID'='99'
  -e 'PGID'='100'
  -e 'TZ'='Europe/Berlin'
  -e 'TAUTULLI_URL'='http://192.168.178.29:8181/api/v2'
  -e 'TAUTULLI_API_KEY'='68xxxxxxxxxxxxxxxxx73'
  -e 'OVERSEERR_URL'='http://192.168.178.29:5055/api/v1'
  -e 'OVERSEERR_API_KEY'='MZZxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxNZz='
  -e 'OVERSEERR_NUM_OF_HISTORY_RECORDS'='200'
  -e 'REQUEST_URL'='https://xyz.xyz.de'
  -e 'THEMOVIEDB_API_KEY'='1xxxxxxxxxxxxxxxxxxxxxxxxxxxx5'
  -e 'SMTP_SERVER'='xxxxxxxxx'
  -e 'SMTP_PORT'='587'
  -e 'SMTP_ENCRYPTION'='STARTTLS'
  -e 'SMTP_USERNAME'='smtp-login@xyz.de'
  -e 'FROM_NAME'='Plex'
  -e 'FROM_EMAIL_ADDRESS'='ab@xyz.de'
  -e 'EMAIL_PASSWORD'='xxxxxxxxxxxxxxxxxx'
  -e 'BCC_EMAIL_ADDRESS'='email@gmail.com'
  -e 'ADMIN_NAME'='Admin'
  -e 'DAYS_SINCE_REQUEST'='90'
  -e 'DAYS_SINCE_REQUEST_EMAIL_TEXT'='3 Months'
  -e 'HOURS_BETWEEN_EMAILS'='48'
  -e 'JOB_INTERVAL_SECONDS'='3600'
  -e 'INITIAL_DELAY_SECONDS'='600'
  -e 'FLASK_SECRET_KEY'='119cxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx772'
  -e 'LOG_LEVEL'='INFO'
  -e 'DEBUG_MODE'='false'
  -e 'DEBUG_MAX_EMAILS'='2'
  -e 'DEBUG_EMAIL'='xyz@xyz.com'
  -e 'DISABLE_SCHEDULER'='true'
  -e 'restart'='unless-stopped'
  -l net.unraid.docker.managed=dockerman
  -l net.unraid.docker.webui='http://[IP]:[PORT:8741]'
  -l net.unraid.docker.icon='https://raw.githubusercontent.com/PyroghostX/ForgottenMovies/refs/heads/main/files/logo.png'
  -p '8741:8741/tcp'
  -v '/mnt/cache/appdata/mediaserver/forgotten-movies':'/app/data':'rw'
  --restart=unless-stopped 'pyroghostx/forgottenmovies:latest'
  ```


# Customising the Email Template

1. In your /app/data folder Copy the "email_template_original.html" and name it "email_template.html"
2. The job automatically reloads the template when the file changes—no restart required.
3. If the template is missing, empty, or Jinja raises an error, the job exits with a clear message so you can fix the template.
4. Update the docker variables `DAYS_SINCE_REQUEST_EMAIL_TEXT` if you change `DAYS_SINCE_REQUEST` so the copy matches the actual delay.

Helpful context variables available inside the template:

| Variable | Meaning |
|----------|---------|
| `plex_username` | Plex username of the requester. |
| `media_type` | `"movie"` or `"tv show"`. |
| `title` | Title retrieved from Overseerr/Tautulli. |
| `time_since_text` | Human-readable string such as `"3 months"`. |
| `plex_url` | Deep link to the title on Plex (desktop/web). |
| `mobile_url` | Optional Plex mobile deep link. |
| `poster_url` | Poster artwork URL (if available). |
| `request_url` | Link back to your request portal (may be empty). |
| `admin_name` | Value of `ADMIN_NAME`. |
| `unsubscribe_url` | encrypted unsubscribe link (empty when feature disabled). |

For example: The {{ media_type }} <strong>{{ title }}</strong> that you requested was added about {{ time_since_text }} ago but you haven't watched it yet.
            Want to give it a watch?.
            Because the template uses Jinja, you can wrap sections in `{% if plex_url %}...{% endif %}` to hide buttons or images when data is missing.


# Self-Service Unsubscribe (Optional)

When enabled, reminder emails include an unsubscribe link that lets users manage their subscription without admin intervention. The feature uses cryptographically signed tokens so links can't be forged without the secret key.

## Enabling the Feature

1. **Generate a secret key:**
   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```

2. **Set both environment variables:**
   ```yaml
   UNSUBSCRIBE_SECRET_KEY: "your-64-character-hex-key"
   BASE_URL: "https://forgotten.example.com"
   ```

3. **Restart the container.** When enabled, reminder emails will include an unsubscribe link in the footer and email headers.

When disabled (default), emails send without unsubscribe links and the endpoints return 404.

## Email Headers & Deliverability

When enabled, reminder emails include RFC 8058 compliant headers for one-click unsubscribe:

```
List-Unsubscribe: <https://forgotten.example.com/unsubscribe/token>
List-Unsubscribe-Post: List-Unsubscribe=One-Click
```

These headers allow Gmail, Apple Mail, and other clients/providers to display a native "Unsubscribe" button in their UI. The headers are only added when `BASE_URL` uses HTTPS (required by RFC 8058).

**For optimal inbox placement**, ensure your sending domain has proper email authentication, verfication, and policies:

- **SPF** - Authorizes your mail server to send on behalf of your domain
- **DKIM** - Cryptographically signs emails to verify authenticity
- **DMARC** - Policy telling receivers how to handle SPF/DKIM failures

> **Note:** Even with proper configuration, email providers may not display the one-click unsubscribe button based on sender reputation, spam scores, or other filtering criteria. The in-email unsubscribe link will always work regardless.

**References:**
- [RFC 8058 - One-Click Unsubscribe](https://datatracker.ietf.org/doc/html/rfc8058)
- [Google Email Sender Guidelines](https://support.google.com/mail/answer/81126)
- [Apple iCloud Mail Postmaster Information](https://support.apple.com/en-us/102322)

## Reverse Proxy Configuration

The unsubscribe/resubscribe endpoints (`/unsubscribe/<token>` and `/resubscribe/<token>`) should be publicly accessible, but you probably want to hide the admin dashboard from the internet.

**Example nginx configuration for subscription endpoints**:
```nginx
server {
    listen 443 ssl;
    server_name forgotten.*;
    include /config/nginx/ssl.conf;

    # Public endpoints - no auth required
    location ~ ^/(unsubscribe|resubscribe)/[^/]+$ {
        include /config/nginx/proxy.conf;
        include /config/nginx/resolver.conf;
        proxy_pass http://forgotten-movies:8741;
    }

    # Everything else returns 404 (hides admin interface)
    location / {
        return 404;
    }
}
```

### Rate Limiting (Optional but Recommended)

If you're exposing the unsubscribe endpoints publicly via nginx or other, add rate limiting to prevent DoS attacks.

#### Example 1: Public unsubscribe endpoints only (admin hidden)

```nginx

# Rate limiting zone - 20 requests per minute per IP
# Place this OUTSIDE the server block (at the top of the file)
limit_req_zone $binary_remote_addr zone=unsubscribe_limit:10m rate=20r/m;
limit_req_status 429;

server {
    listen 443 ssl;
    server_name forgotten.*;
    include /config/nginx/ssl.conf;

    client_max_body_size 0;

    # Public endpoints with rate limiting
    location ~ ^/(unsubscribe|resubscribe)/[^/]+$ {
        # Apply rate limiting (burst=5 allows brief spikes)
        limit_req zone=unsubscribe_limit burst=5 nodelay;

        include /config/nginx/proxy.conf;
        include /config/nginx/resolver.conf;
        proxy_pass http://forgotten-movies:8741;
    }

    # Everything else returns 404 (hides admin interface)
    location / {
        return 404;
    }
}
```

#### Example 2: Public unsubscribe endpoints + proxied admin (with auth)

```nginx

# Rate limiting zone - 20 requests per minute per IP
limit_req_zone $binary_remote_addr zone=unsubscribe_limit:10m rate=20r/m;
limit_req_status 429;

server {
    listen 443 ssl;
    server_name forgotten.*;
    include /config/nginx/ssl.conf;

    client_max_body_size 0;

    # Public endpoints with rate limiting (no auth required)
    location ~ ^/(unsubscribe|resubscribe)/[^/]+$ {
        # Apply rate limiting (burst=5 allows brief spikes)
        limit_req zone=unsubscribe_limit burst=5 nodelay;

        include /config/nginx/proxy.conf;
        include /config/nginx/resolver.conf;
        proxy_pass http://forgotten-movies:8741;
    }

    # Admin interface (authenticated, NO rate limiting)
    location / {
        # Require authentication for admin pages
        include /config/nginx/proxy.conf;
        include /config/nginx/resolver.conf;
        proxy_pass http://forgotten-movies:8741;

        # Add your favorite auth provider or use basic auth
        auth_basic "Forgotten Movies Admin";
        auth_basic_user_file /config/nginx/.htpasswd;
    }
}
```

# UI Tour

- **Dashboard (`/`)** - Run the job manually, review the upcoming reminder queue (oldest requests first), see the most recent reminder emails, and manage the unsubscribe list.
- **Logs (`/logs`)** - Live tail of the application log with controls to change the log level, clear the log files, and toggle auto-refresh.
- **Settings (`/settings`)** - Enable or disable the background scheduler that performs automated API calls and sends reminder emails.


# Support

- For now keep all questions and suggestions in github, if this grows enough then I may make a subreddit or discord channel.


# Debugging & Operations

- Set `DEBUG_MODE=true` to reroute mail to `DEBUG_EMAIL` (or `FROM_EMAIL_ADDRESS` if `DEBUG_EMAIL` is blank) and cap sends via `DEBUG_MAX_EMAILS`.
- Logs rotate when they reach `LOG_FILE_MAX_BYTES`. Adjust or disable rotation via environment variables if needed.

## Reading the logs

- "Registered email user": You'll see this for each new email it finds in the overseerr requests.

- "smtplib.SMTPAuthenticationError: (535, b'5.7.8 Username and Password not accepted.": Your email or password is wrong, check email address and make sure you setup an app password https://myaccount.google.com/apppasswords


# Architecture Overview

| Component | Purpose |
|-----------|---------|
| `forgotten_movies.py` | Core job. Loads Overseerr requests, checks Tautulli watch history, builds emails from the template, tracks state in TinyDB. |
| `webapp.py` | Flask UI for manual runs, queue visibility, logs, and settings. Manual runs now defer to an inter-process lock so they play nicely with the scheduler. |
| `scheduler_runner.py` | Standalone process that wakes up every `JOB_INTERVAL_SECONDS`, respects the TinyDB disable flag, and triggers the core job if the lock is free. |
| `job_runner.py` | Shared helpers that wrap the core job with logging, lock acquisition, and log flushing. |
| `entrypoint.py` | Lightweight supervisor that starts both the scheduler process and Gunicorn, forwarding signals so the container restarts cleanly. |
| TinyDB (`/app/data/*.json`) | Stores Overseerr request metadata, email history, and unsubscribe list. |
| `templates/email_template.html` | HTML reminder template. Copied to `/app/data/email_template_original.html` on start; `/app/data/email_template.html` overrides if present. |
| `templates/base.html` et al. | Shared layout, dashboard, and log templates for the web UI. |
| `docker-compose.yml` | Opinionated container configuration: single service running the supervisor entrypoint with a bind-mounted data directory. |

Everything that changes at runtime lives under `/app/data` so you can back it up or mount it from the host.

## Contributing

Issues and pull requests are welcome. If you add new template placeholders or environment variables, please document them in this README so operators can configure them correctly.

## License

This is free software under the GPL v3 open source license.
