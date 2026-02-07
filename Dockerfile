FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY monitor.py ./

RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /state \
    && chown -R appuser:appuser /app /state

RUN set -eux; \
    printf '%s\n' \
      '#!/bin/sh' \
      'set -eu' \
      '' \
      'APP_USER="${APP_USER:-appuser}"' \
      'APP_GROUP="${APP_GROUP:-appuser}"' \
      'STATE_FILE_PATH="${STATE_FILE:-/state/position.txt}"' \
      'STATE_DIR="$(dirname "$STATE_FILE_PATH")"' \
      '' \
      'if [ "$(id -u)" -eq 0 ]; then' \
      '  mkdir -p "$STATE_DIR"' \
      '  if ! chown -R "${APP_USER}:${APP_GROUP}" "$STATE_DIR" /app 2>/dev/null; then' \
      '    echo "[warn] Failed to adjust ownership for $STATE_DIR; monitor may switch to fallback state file."' \
      '  fi' \
      '  exec gosu "${APP_USER}:${APP_GROUP}" "$@"' \
      'fi' \
      '' \
      'exec "$@"' \
      > /usr/local/bin/docker-entrypoint.sh \
    && chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["docker-entrypoint.sh"]

CMD ["python", "-u", "monitor.py"]
