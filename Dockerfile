# Use official lightweight Python image
FROM python:3.11-slim

# Prevent Python from writing .pyc files & enable unbuffered stdout/stderr logging
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

WORKDIR /app

# Install system build dependencies required for compiling binary extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency manifests
COPY requirements.txt .

# Install Python production dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Expose port
EXPOSE 8080

# Start Uvicorn listening on Cloud Run's dynamic $PORT with proxy-headers enabled
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*'"]
