# Use the official lightweight Python image
FROM python:3.12-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Set the working directory
WORKDIR /app

# System dependencies:
#   gosu   - drop from root to the runtime user at startup
#   passwd - provides usermod/groupmod so the entrypoint can honor PUID/PGID
# Also create the runtime user/group (remapped at runtime by the entrypoint).
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu passwd \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1000 appuser \
    && useradd -u 1000 -g appuser -d /app -s /usr/sbin/nologin appuser

# Copy the requirements file into the container
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application files
COPY . .
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# Ensure the data directory exists and the entrypoint is executable
RUN mkdir -p /app/data \
    && chmod +x /usr/local/bin/docker-entrypoint.sh

# Expose the port for the web UI
EXPOSE 8741

# Container health: the app exposes a lightweight /health endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8741/health', timeout=4).status==200 else 1)"

LABEL org.opencontainers.image.title="Forgotten Movies" \
      org.opencontainers.image.version="0.6.0" \
      org.opencontainers.image.source="https://github.com/PyroghostX/ForgottenMovies"

# The entrypoint drops privileges to PUID/PGID, then runs the CMD.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

# Launch the web app and scheduler supervisor
CMD ["python", "entrypoint.py"]
