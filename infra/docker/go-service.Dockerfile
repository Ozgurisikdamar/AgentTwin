# syntax=docker/dockerfile:1.7
#
# One image per AgentTwin Go service:
#
#   docker build -f infra/docker/go-service.Dockerfile --build-arg SERVICE=trace-service .
#
# The runtime image is distroless (no shell, no package manager) and runs as
# an unprivileged user. `<service> healthcheck` replaces curl in HEALTHCHECK.
#
# Build environments that intercept TLS (corporate proxies, sandboxes) can
# pass their CA without baking it into any layer:
#   --secret id=ca_bundle,src=/path/to/ca.pem
ARG GO_VERSION=1.25

FROM golang:${GO_VERSION}-alpine AS build
ARG SERVICE
ARG VERSION=dev
ARG COMMIT=
WORKDIR /src
ENV CGO_ENABLED=0 GOFLAGS=-trimpath GOTOOLCHAIN=local
COPY go.mod go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=secret,id=ca_bundle,required=false \
    if [ -s /run/secrets/ca_bundle ]; then export SSL_CERT_FILE=/run/secrets/ca_bundle; fi; \
    go mod download
COPY . .
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    test -n "${SERVICE}" || { echo "build-arg SERVICE is required" >&2; exit 1; }; \
    go build -ldflags="-s -w \
      -X github.com/Ozgurisikdamar/AgentTwin/packages/gokit/buildinfo.Version=${VERSION} \
      -X github.com/Ozgurisikdamar/AgentTwin/packages/gokit/buildinfo.Commit=${COMMIT}" \
      -o /out/service ./services/${SERVICE}/cmd/${SERVICE}

FROM gcr.io/distroless/static-debian12:nonroot
ARG SERVICE
LABEL org.opencontainers.image.source="https://github.com/Ozgurisikdamar/AgentTwin" \
      org.opencontainers.image.title="agenttwin-${SERVICE}" \
      org.opencontainers.image.licenses="Apache-2.0"
COPY --from=build /out/service /app/service
USER nonroot:nonroot
ENTRYPOINT ["/app/service"]
HEALTHCHECK --interval=5s --timeout=4s --start-period=10s --retries=24 CMD ["/app/service", "healthcheck"]
