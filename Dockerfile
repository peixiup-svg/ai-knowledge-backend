FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /srv/app

RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app
COPY pyproject.toml README.md constraints.txt ./
COPY app ./app
RUN python -m pip install --no-cache-dir -c constraints.txt .
COPY alembic.ini ./
COPY migrations ./migrations
RUN mkdir -p /srv/app/data/uploads && chown -R app:app /srv/app/data

USER app
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
