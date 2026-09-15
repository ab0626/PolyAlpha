# Polyalpha - Prediction Market Research Platform
# Multi-stage build for production-like image

# Stage 1: Build dependencies
FROM python:3.12-slim AS builder

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency files
COPY pyproject.toml README.md ./
COPY src/ src/

# Install Python dependencies
RUN pip install --no-cache-dir -e ".[dev,yaml]"

# Stage 2: Runtime
FROM python:3.12-slim AS runtime

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY src/ src/
COPY config/ config/
COPY sql/ sql/
COPY scripts/ scripts/

# Create data directory
RUN mkdir -p /app/data

# Set Python path
ENV PYTHONPATH=/app/src

# Default command
CMD ["python", "-m", "polyalpha.cli", "--help"]
