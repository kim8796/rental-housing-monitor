#!/usr/bin/env bash
set -Eeuo pipefail
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin
export PATH
umask 077

if [[ "$(id -u)" -ne 0 ]]; then
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    age \
    ca-certificates \
    docker.io \
    docker-compose-v2 \
    sqlite3
rm -rf /var/lib/apt/lists/*
command -v gcloud >/dev/null 2>&1 || exit 1

if getent group 10001 >/dev/null; then
    [[ "$(getent group 10001 | cut -d: -f1)" == "personal-monitor" ]] || exit 1
else
    groupadd --gid 10001 --system personal-monitor
fi
if getent passwd 10001 >/dev/null; then
    [[ "$(getent passwd 10001 | cut -d: -f1)" == "personal-monitor" ]] || exit 1
else
    useradd \
        --uid 10001 \
        --gid 10001 \
        --system \
        --no-create-home \
        --home-dir /nonexistent \
        --shell /usr/sbin/nologin \
        personal-monitor
fi

install -d -o 10001 -g 10001 -m 0700 /srv/personal-monitor
for directory in \
    app \
    db \
    adaptive \
    vault \
    diagnostics \
    logs \
    backups
do
    install -d -o 10001 -g 10001 -m 0700 "/srv/personal-monitor/$directory"
done
chown 10001:10001 \
    /srv/personal-monitor \
    /srv/personal-monitor/app \
    /srv/personal-monitor/db \
    /srv/personal-monitor/adaptive \
    /srv/personal-monitor/vault \
    /srv/personal-monitor/diagnostics \
    /srv/personal-monitor/logs \
    /srv/personal-monitor/backups
chmod 0700 \
    /srv/personal-monitor \
    /srv/personal-monitor/app \
    /srv/personal-monitor/db \
    /srv/personal-monitor/adaptive \
    /srv/personal-monitor/vault \
    /srv/personal-monitor/diagnostics \
    /srv/personal-monitor/logs \
    /srv/personal-monitor/backups

install -d -o root -g root -m 0700 /etc/personal-monitor
chmod 0700 /etc/personal-monitor

systemctl enable --now docker
