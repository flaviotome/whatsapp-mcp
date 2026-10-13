# syntax=docker/dockerfile:1

# ---- Stage 1: build wacli (Go + cgo/sqlite FTS5) from a pinned upstream release ----
FROM golang:1.27.2-alpine@sha256:85dc1069ac644ea3c527b177303a406eb3358192816cd7f9e5848eb658851673 AS wacli-build
ARG WACLI_REPO=https://github.com/openclaw/wacli.git
ARG WACLI_REF=v0.21.0
RUN apk add --no-cache build-base ca-certificates git
RUN git clone --depth 1 --branch "${WACLI_REF}" "${WACLI_REPO}" /src
WORKDIR /src
RUN go mod download
RUN CGO_ENABLED=1 CGO_CFLAGS="-Wno-error=missing-braces" GOOS=linux \
    go build -tags sqlite_fts5 -trimpath -buildvcs=false \
      -ldflags="-s -w -X main.version=${WACLI_REF#v}" -o /out/wacli ./cmd/wacli

# ---- Stage 2: Python MCP server (Alpine/musl, same libc as the wacli binary) ----
FROM python:3.13-alpine
RUN apk add --no-cache ca-certificates tzdata \
    && adduser -D -u 10001 -h /home/app app \
    && mkdir -p /data/store \
    && chown -R app:app /data
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY --from=wacli-build /out/wacli /usr/local/bin/wacli
COPY --from=wacli-build /src/LICENSE /usr/share/licenses/wacli/LICENSE

ENV HOME=/home/app \
    WACLI_STORE_DIR=/data/store \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000
VOLUME ["/data"]
EXPOSE 8000
USER app
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD wget -qO- http://127.0.0.1:8000/health >/dev/null || exit 1
CMD ["python", "-m", "app"]
