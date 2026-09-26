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
# pip ships with the base image but nothing installs packages at run time
# (the venv is copied in whole), so it goes (docs/security/supply-chain.md).
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app \
 && PIP_ROOT_USER_ACTION=ignore python -m pip uninstall --yes --quiet pip
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
USER 10001:10001

# The demo agent, its tools and the traffic generator (demo/support-refund-agent).
# The assurance assets (tool twin + scenarios) are registered by `seed`.
FROM runtime AS support-refund-agent
COPY demo/support-refund-agent/manifests /app/manifests
COPY demo/support-refund-agent/assurance /app/assurance
ENV DEMO_AGENT_MANIFEST_DIR=/app/manifests DEMO_ASSURANCE_DIR=/app/assurance
ENTRYPOINT ["support-refund-agent"]
CMD ["--help"]

# The simulation service (PACKAGE=agenttwin-simulation). One image, two roles:
# `serve` (API + twin endpoint) and `worker` (runs the simulations).
FROM runtime AS simulation-service
COPY packages/contracts /app/schemas/contracts
COPY packages/scenario-schema /app/schemas/scenario-schema
COPY services/simulation-service/migrations /app/services/simulation-service/migrations
ENV AGENTTWIN_SCHEMA_DIR=/app/schemas
HEALTHCHECK --interval=5s --timeout=5s --start-period=10s --retries=24 CMD ["simulation-service", "healthcheck"]
ENTRYPOINT ["simulation-service"]
CMD ["serve"]

# The evaluation service (PACKAGE=agenttwin-evaluation). One image, two roles:
# `serve` (datasets, evaluation runs, reviews, judge calibrations) and
# `worker` (prepares, waits for and evaluates runs; runs calibrations).
FROM runtime AS evaluation-service
COPY packages/contracts /app/schemas/contracts
# Promoted regressions are validated as scenarios before they are registered.
COPY packages/scenario-schema /app/schemas/scenario-schema
COPY services/evaluation-service/migrations /app/services/evaluation-service/migrations
ENV AGENTTWIN_SCHEMA_DIR=/app/schemas
HEALTHCHECK --interval=5s --timeout=5s --start-period=10s --retries=24 CMD ["evaluation-service", "healthcheck"]
ENTRYPOINT ["evaluation-service"]
CMD ["serve"]
