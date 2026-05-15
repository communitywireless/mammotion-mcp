FROM python:3.12-slim

# System deps (curl for healthcheck-style probes if needed)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first for layer caching
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e .

# Application source
COPY mammotion_mcp ./mammotion_mcp
COPY data ./data

# MCP servers run via stdio — no port exposure needed.
# The container is invoked with stdio attached by the consumer agent's MCP client.

ENTRYPOINT ["python", "-m", "mammotion_mcp.server"]
