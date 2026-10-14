

FROM irisxdr/neo-wzml:latest

WORKDIR /usr/src/app

RUN chmod 777 /usr/src/app

# qBittorrent-nox upgrade (static build). Replaces old `neobit` binary from base image.
# QBIT_TAG: "latest" or pinned tag e.g. release-5.2.3_v2.0.13 (see userdocs/qbittorrent-nox-static releases)
ARG QBIT_TAG=latest
ARG QBIT_MIN_VERSION=5.2.3
USER root
RUN set -eux; \
    arch="$(uname -m)"; \
    case "$arch" in x86_64) asset=x86_64-qbittorrent-nox ;; aarch64) asset=aarch64-qbittorrent-nox ;; *) echo "unsupported arch $arch"; exit 1 ;; esac; \
    if [ "$QBIT_TAG" = "latest" ]; then url="https://github.com/userdocs/qbittorrent-nox-static/releases/latest/download/$asset"; \
    else url="https://github.com/userdocs/qbittorrent-nox-static/releases/download/$QBIT_TAG/$asset"; fi; \
    curl -fsSL "$url" -o /tmp/neobit; \
    chmod +x /tmp/neobit; \
    ver="$(/tmp/neobit --version | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"; \
    echo "qBittorrent $ver"; \
    [ "$(printf '%s\n%s\n' "$QBIT_MIN_VERSION" "$ver" | sort -V | head -1)" = "$QBIT_MIN_VERSION" ] || { echo "qBittorrent $ver < $QBIT_MIN_VERSION"; exit 1; }; \
    install -m 755 /tmp/neobit "$(command -v neobit || echo /usr/local/bin/neobit)"; \
    rm -f /tmp/neobit

COPY requirements.txt .
RUN uv pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["bash", "start.sh"]
