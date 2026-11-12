FROM python:3.13-slim
LABEL org.opencontainers.image.title="openbrisket" \
      org.opencontainers.image.description="BBQ as code: recipes and rubs in git, a cook log with history, and notes when the meat needs you" \
      org.opencontainers.image.source="https://github.com/quinneydavid/openbrisket" \
      org.opencontainers.image.licenses="MIT"
# git keeps the cook log; PyYAML is the only Python dependency.
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "PyYAML==6.0.2"
RUN mkdir -p /data && chown 1000:1000 /data
COPY recipes /app/recipes
COPY openbrisket /app/openbrisket
WORKDIR /app
USER 1000:1000
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp \
    OPENBRISKET_HOME=/data OPENBRISKET_HOST=0.0.0.0 OPENBRISKET_PORT=8321
VOLUME /data
EXPOSE 8321/tcp
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8321/healthz', timeout=4).status == 200 else 1)"
ENTRYPOINT ["python3", "-m", "openbrisket", "serve"]
