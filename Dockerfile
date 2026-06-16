FROM python:3.12-slim

WORKDIR /app

# Install system deps needed for some optional packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY bridge/       ./bridge/
COPY pyproject.toml requirements*.txt ./

# Install bridge deps — openai + anthropic + bedrock, no local embed
# (use OpenAI text-embedding-3-small inside Docker; avoids the ~1GB torch dep)
RUN pip install --no-cache-dir -e ".[openai,anthropic,bedrock]"

# Cache directory (mount a volume here to persist Book Index data across restarts)
RUN mkdir -p /root/.marginalia/cache

EXPOSE 7731

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -sf http://localhost:7731/ping || exit 1

CMD ["python", "-u", "bridge/server.py"]
