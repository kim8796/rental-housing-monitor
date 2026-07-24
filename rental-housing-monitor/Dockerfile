FROM python:3.12.11-slim-bookworm AS wheel-builder

WORKDIR /build
COPY pyproject.toml /build/pyproject.toml
COPY src/ /build/src/
RUN python -m pip install --no-cache-dir "build==1.2.2.post1" \
    && python -m build --wheel --outdir /dist \
    && python -m pip wheel --wheel-dir /wheelhouse /dist/*.whl

FROM node:22.17.0-bookworm-slim AS node-runtime

FROM python:3.12.11-slim-bookworm AS runtime

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/personal-monitor/browsers

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        age \
        ca-certificates \
        curl \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libcairo2 \
        libcups2 \
        libdbus-1-3 \
        libdrm2 \
        libgbm1 \
        libglib2.0-0 \
        libgtk-3-0 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libx11-6 \
        libx11-xcb1 \
        libxcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        novnc \
        sqlite3 \
        websockify \
        x11vnc \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node-runtime /usr/local/ /usr/local/
COPY --from=wheel-builder /wheelhouse/ /wheelhouse/
RUN python -m pip install --no-cache-dir \
        --no-index \
        --find-links=/wheelhouse \
        "rental-housing-monitor==0.1.0" \
    && npm install --global @openai/codex@0.144.1 \
    && scrapling install --force \
    && chmod -R a+rX /opt/personal-monitor/browsers \
    && groupadd --gid 10001 personal-monitor \
    && useradd \
        --uid 10001 \
        --gid 10001 \
        --no-create-home \
        --shell /usr/sbin/nologin \
        personal-monitor \
    && mkdir -p \
        /srv/personal-monitor/db \
        /srv/personal-monitor/adaptive \
        /srv/personal-monitor/vault \
        /srv/personal-monitor/diagnostics \
        /srv/personal-monitor/logs \
        /srv/personal-monitor/codex-home \
        /srv/personal-monitor/profile-bootstrap \
        /run/personal-monitor-ai \
    && chown -R 10001:10001 \
        /srv/personal-monitor \
        /run/personal-monitor-ai \
    && chmod 0700 \
        /srv/personal-monitor \
        /srv/personal-monitor/db \
        /srv/personal-monitor/adaptive \
        /srv/personal-monitor/vault \
        /srv/personal-monitor/diagnostics \
        /srv/personal-monitor/logs \
        /srv/personal-monitor/codex-home \
        /srv/personal-monitor/profile-bootstrap \
        /run/personal-monitor-ai \
    && rm -rf /wheelhouse

COPY --chown=0:0 --chmod=0555 deploy/entrypoint.sh /usr/local/bin/personal-monitor-entrypoint

USER 10001:10001
ENTRYPOINT ["/usr/local/bin/personal-monitor-entrypoint"]
CMD ["personal-monitor", "--help"]
