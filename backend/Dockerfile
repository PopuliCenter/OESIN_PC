FROM python:3.12-slim

WORKDIR /app

# psycopg[binary] bundles libpq, so no system packages are needed.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# --reload is convenient in dev because the code is bind-mounted by compose.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
