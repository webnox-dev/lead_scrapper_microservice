FROM python:3.12-slim-bookworm

WORKDIR /app

# Install system dependencies (including build tools for native compilation)
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    make \
    curl \
    wget \
    --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Copy python packaging/project files
COPY pyproject.toml .
COPY requirements.txt .

# Remove local path dependency from requirements.txt to prevent build failure
RUN sed -i '/file:\/\/\//d' requirements.txt

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir .

# Install Playwright chromium browser and its OS system dependencies
RUN playwright install chromium
RUN playwright install-deps chromium

# Copy code
COPY . .

# Expose port (FastAPI app default)
EXPOSE 8000

# Start uvicorn
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
