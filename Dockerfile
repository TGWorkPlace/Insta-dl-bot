FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install ffmpeg + ffprobe (needed for thumbnail/duration extraction)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    gcc \
    && rm -rf /var/lib/apt/lists/*


# Copy and install Python dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot source
COPY . .

# Expose Koyeb health-check port
EXPOSE 8080

# Run the bot
CMD ["python", "bot.py"]
