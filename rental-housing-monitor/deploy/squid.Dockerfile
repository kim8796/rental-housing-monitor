FROM ubuntu:24.04

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        squid \
    && rm -rf /var/lib/apt/lists/*

COPY deploy/squid.conf /etc/squid/squid.conf

RUN squid -k parse -f /etc/squid/squid.conf

USER proxy:proxy
CMD ["squid", "-N", "-f", "/etc/squid/squid.conf"]
