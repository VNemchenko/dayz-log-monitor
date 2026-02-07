FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY monitor.py .

RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /state \
    && chown -R appuser:appuser /app /state

USER appuser

CMD ["python", "-u", "monitor.py"]
