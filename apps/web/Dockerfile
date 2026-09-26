# syntax=docker/dockerfile:1.7
#
# AgentTwin web app (Next.js standalone server). Build from the repo root:
#
#   docker build -f apps/web/Dockerfile .
#
# TLS-intercepting build environments can pass their CA as a build secret:
#   --secret id=ca_bundle,src=/path/to/ca.pem
ARG NODE_VERSION=22

FROM node:${NODE_VERSION}-alpine AS build
WORKDIR /repo
ENV NEXT_TELEMETRY_DISABLED=1 COREPACK_ENABLE_DOWNLOAD_PROMPT=0
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json apps/web/
COPY packages/scenario-schema/package.json packages/scenario-schema/
RUN --mount=type=cache,id=pnpm-store,target=/root/.local/share/pnpm/store \
    --mount=type=secret,id=ca_bundle,required=false \
    if [ -s /run/secrets/ca_bundle ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/ca_bundle; fi; \
    corepack enable && pnpm install --frozen-lockfile --filter @agenttwin/web...
COPY apps/web apps/web
RUN pnpm --filter @agenttwin/web build

FROM node:${NODE_VERSION}-alpine
LABEL org.opencontainers.image.source="https://github.com/Ozgurisikdamar/AgentTwin" \
      org.opencontainers.image.title="agenttwin-web" \
      org.opencontainers.image.licenses="Apache-2.0"
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
# The server only needs node. npm, npx, corepack and yarn ship with the base
# image, are never run here and carry their own dependency trees, which the
# image scan reports (docs/security/supply-chain.md).
RUN rm -rf /usr/local/lib/node_modules /opt/yarn-v* \
      /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack \
      /usr/local/bin/yarn /usr/local/bin/yarnpkg
WORKDIR /app
COPY --from=build --chown=node:node /repo/apps/web/.next/standalone ./
COPY --from=build --chown=node:node /repo/apps/web/.next/static ./apps/web/.next/static
COPY --from=build --chown=node:node /repo/apps/web/public ./apps/web/public
USER node
EXPOSE 3000
HEALTHCHECK --interval=5s --timeout=4s --start-period=10s --retries=24 \
  CMD ["node", "-e", "fetch('http://127.0.0.1:3000/healthz').then(r=>process.exit(r.ok?0:1),()=>process.exit(1))"]
CMD ["node", "apps/web/server.js"]
