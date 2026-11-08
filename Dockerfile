FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    FULLMCP_TRANSPORT=http \
    FULLMCP_HOST=0.0.0.0 \
    FULLMCP_PORT=8000 \
    FULLMCP_FILES_DIR=/data/fullmcp-files

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY fullmcp_server ./fullmcp_server
RUN pip install --no-cache-dir . \
    && useradd --system --uid 10001 fullmcp \
    && mkdir -p /data/fullmcp-files \
    && chown -R fullmcp /data

USER fullmcp
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ['FULLMCP_PORT'], timeout=3)"

CMD ["fullmcp-server"]
