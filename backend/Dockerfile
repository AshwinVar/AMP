# FlowMES backend — containerised FastAPI app.
# Lets the backend run anywhere Docker runs (Railway, Render, a VPS, k8s later).
FROM python:3.11-slim

WORKDIR /app

# Install dependencies first so this layer is cached across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App source.
COPY . .

# Railway/host injects PORT; default to 8080 for local runs.
EXPOSE 8080
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
