FROM python:3.12-slim

# psycopg2 needs libpq at runtime; the -binary wheel bundles the rest.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, so a code change does not reinstall them.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Not root: a compromise in the app should not own the container.
RUN useradd --create-home --uid 10001 apsara && chown -R apsara:apsara /app
USER apsara

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

# The same image runs both processes — the worker overrides this command.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
