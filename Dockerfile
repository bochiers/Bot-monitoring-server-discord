# Menggunakan Python 3.12 slim sebagai base image yang ringan dan efisien
FROM python:3.12-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Set working directory
WORKDIR /app

# Copy requirements terlebih dahulu untuk mengoptimalkan layer caching
COPY requirements.txt .

# Install dependencies Python
RUN pip install --no-cache-dir -r requirements.txt

# Buat non-root user untuk keamanan
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Copy seluruh source code project
COPY --chown=appuser:appuser . .

# Default command: menjalankan interactive discord bot & background monitoring
CMD ["python", "bot.py"]
