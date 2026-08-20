# Use official lightweight Python image
FROM python:3.11-slim

# Prevent Python from writing .pyc files & enable unbuffered stdout/stderr logging
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

WORKDIR /app

# Install system build dependencies required for compiling crypto/bcrypt binaries
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency manifests first to leverage Docker layer caching
COPY requirements.txt .

# Install Python production dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy local application code into container
COPY . .

# Google Cloud Run default port
EXPOSE 8080

# Run FastAPI with Uvicorn using Cloud Run's dynamic $PORT
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}
