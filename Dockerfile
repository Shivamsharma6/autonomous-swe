FROM python:3.11-slim

WORKDIR /app

# Install system utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    ripgrep \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code & workspace files
COPY pyproject.toml .
COPY README.md .
COPY autoswe/ ./autoswe/
COPY games_demo/ ./games_demo/
COPY tests/ ./tests/

# Install package in editable mode
RUN pip install --no-cache-dir -e .

EXPOSE 8000

CMD ["uvicorn", "autoswe.control_plane:app", "--host", "0.0.0.0", "--port", "8000"]
