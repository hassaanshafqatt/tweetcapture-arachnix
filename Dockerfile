FROM python:3.11.5-alpine

# Install system dependencies
RUN apk add --update --update-cache \
    chromium \
    chromium-chromedriver \
    git \
    && rm -rf /var/cache/apk/*

WORKDIR /app

# Copy requirements first for better Docker layer caching
COPY requirements.txt .

# Install Python dependencies (this will install tweetcapture from PyPI)
RUN pip install -r requirements.txt && \
    pip install git+https://github.com/xacnio/tweetcapture.git

# Copy ONLY the FastAPI application (not the conflicting screenshot.py)
COPY main.py .

# Set environment variables for Chrome
ENV CHROME_DRIVER=/usr/bin/chromedriver

# Expose the port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=30s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Run the FastAPI application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]