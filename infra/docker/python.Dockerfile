# syntax=docker/dockerfile:1.7
#
# Images for the Python members of the uv workspace (ADR-0010). The build
# stage installs one package (and its workspace dependencies) non-editably
# from the lockfile; each deployable is a target:
#
#   docker build -f infra/docker/python.Dockerfile \
#     --build-arg PACKAGE=support-refund-agent --build-arg EXTRAS=anthropic \
#     --target support-refund-agent .
#
# EXTRAS is a space-separated list of the package's optional dependency groups.
#
# TLS-intercepting build environments can pass their CA as a build secret:
#   --secret id=ca_bundle,src=/path/to/ca.pem
ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS build
ARG PACKAGE
ARG EXTRAS=
# Matches the uv that wrote uv.lock; bump both together.
ARG UV_VERSION=0.8.17
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ -s /run/secrets/ca_bundle ]; then export PIP_CERT=/run/secrets/ca_bundle; fi; \
    pip install --no-cache-dir "uv==${UV_VERSION}"
WORKDIR /src
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=secret,id=ca_bundle,required=false \
    if [ -s /run/secrets/ca_bundle ]; then export SSL_CERT_FILE=/run/secrets/ca_bundle; fi; \
    test -n "${PACKAGE}" || { echo "build-arg PACKAGE is required" >&2; exit 1; }; \
    extras=""; for e in ${EXTRAS}; do extras="${extras} --extra ${e}"; done; \
    uv sync --frozen --no-dev --no-editable --package "${PACKAGE}" ${extras}

FROM python:${PYTHON_VERSION}-slim AS runtime
LABEL org.opencontainers.image.source="https://github.com/Ozgurisikdamar/AgentTwin" \
      org.opencontainers.image.licenses="Apache-2.0"
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
USER 10001:10001

# The demo agent, its tools and the traffic generator (demo/support-refund-agent).
FROM runtime AS support-refund-agent
COPY demo/support-refund-agent/manifests /app/manifests
ENV DEMO_AGENT_MANIFEST_DIR=/app/manifests
ENTRYPOINT ["support-refund-agent"]
CMD ["--help"]
