FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
 && rm -rf tests infra deploy .github

USER 10001
EXPOSE 8000
# Eén proces met threads: de rate-limiter en de Odoo-verbindingscache zijn per proces.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", \
     "--timeout", "120", "--forwarded-allow-ips", "*", "--access-logfile", "-", "app:app"]
