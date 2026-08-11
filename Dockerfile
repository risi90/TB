# Production image for the trading bot and its Streamlit dashboard.
#
# The same image serves both docker-compose services:
#   bot:       python main.py                     (default CMD)
#   dashboard: streamlit run dashboard/app.py ... (command override)
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first so source edits don't bust this layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Runtime state (SQLite database, historical cache) and logs live in
# volume-mounted directories so they persist across container rebuilds.
RUN mkdir -p /app/data /app/logs

# Streamlit dashboard port.
EXPOSE 8501

CMD ["python", "main.py"]
