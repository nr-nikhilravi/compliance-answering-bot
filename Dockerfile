FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    PORT=8080 \
    APP_BASE_DIR=/app

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Ensure necessary directories exist so the app doesn't crash on startup
RUN mkdir -p "Input Excel" "Output Excel" ".rag_cache" "Existing knowledge/Compliances"

# Expose port (Cloud Run automatically sets the PORT environment variable)
EXPOSE 8080

# Start Uvicorn, binding to the injected PORT
CMD ["sh", "-c", "uvicorn web:app --host 0.0.0.0 --port ${PORT:-8080}"]
