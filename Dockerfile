# ACP-Ant in Docker. Everything that must survive a rebuild lives in the /data volume:
# gateway.env (API key), accounts/ (Google logins + proxies), panel.json, stats, and the official agy CLI itself (bin/).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ACPANT_INSTALL=docker \
    AGY_ROOT=/data \
    AGY_HOST=0.0.0.0 \
    AGY_PORT=8095

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl procps \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 1000 --shell /bin/bash acpant

WORKDIR /app
COPY requirements.txt /app/
RUN pip install -r requirements.txt

COPY acp_ant.py acp_panel.py panel.html CHANGELOG.md /app/
COPY scripts/ /app/scripts/
COPY docker/entrypoint.sh /usr/local/bin/acp-ant-entrypoint
COPY docker/account.sh /usr/local/bin/account.sh
RUN chmod 755 /usr/local/bin/acp-ant-entrypoint /usr/local/bin/account.sh \
 && mkdir -p /usr/local/lib/acp-ant \
 && sed -e 's#^ROOT=${AGY_ROOT:-/opt/acp-ant}#ROOT=${AGY_ROOT:-/data}#' /app/scripts/account.sh > /usr/local/lib/acp-ant/account.sh \
 && chmod 755 /usr/local/lib/acp-ant/account.sh

VOLUME /data
EXPOSE 8095
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${AGY_PORT}/healthz" >/dev/null || exit 1
ENTRYPOINT ["acp-ant-entrypoint"]
