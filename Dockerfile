FROM python:3.12-slim

# System deps (curl for healthcheck-style probes if needed)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first for layer caching.
# pyproject.toml declares readme = "README.md" + hatch builds an editable
# wheel from the `mammotion_mcp` package — both must be present at install
# time, so copy them BEFORE `pip install -e .`.
COPY pyproject.toml README.md ./
COPY mammotion_mcp ./mammotion_mcp
RUN pip install --no-cache-dir -e .

# Application data (kept as a separate layer so updating area-mapping.json
# doesn't invalidate the pip-install layer).
COPY data ./data

# MCP servers run via stdio — no port exposure needed.
# The container is invoked with stdio attached by the consumer agent's MCP client.

ENTRYPOINT ["python", "-m", "mammotion_mcp.server"]
