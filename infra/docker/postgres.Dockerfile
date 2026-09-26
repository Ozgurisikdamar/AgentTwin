# syntax=docker/dockerfile:1.7
#
# PostgreSQL 16 with pgvector: the database of every AgentTwin service.
#
# The published pgvector image trails Debian's security updates; this image
# applies them at build time (docs/security/supply-chain.md). Only Debian's
# packages are upgraded: PostgreSQL and pgvector stay at the versions the tag
# names.
#
# apt goes over HTTPS, checked against the public CA bundle of the distroless
# image, or against the CA a TLS-intercepting build proxy passes as a secret:
#   --secret id=ca_bundle,src=/path/to/ca.pem --build-arg HTTPS_PROXY=...
#
# An index that cannot be downloaded fails the build (--error-on=any). Without
# it apt only warns, finds nothing to upgrade and the build succeeds with every
# vulnerable package still in place. The PGDG repository is left out of the
# upgrade so PostgreSQL itself is not moved to a newer minor release by a
# rebuild. apt downloads as the unprivileged _apt user, so the CA secret is
# mounted world-readable (it is a public certificate, not a key).
FROM pgvector/pgvector:0.8.6-pg16-trixie
ARG HTTPS_PROXY=
RUN --mount=type=secret,id=ca_bundle,required=false,mode=0444 \
    --mount=type=bind,from=gcr.io/distroless/static-debian12:nonroot,source=/etc/ssl/certs/ca-certificates.crt,target=/tmp/public-ca.crt \
    set -eu; \
    ca=/tmp/public-ca.crt; if [ -s /run/secrets/ca_bundle ]; then ca=/run/secrets/ca_bundle; fi; \
    set -- -o "Acquire::https::CAInfo=${ca}"; \
    if [ -n "${HTTPS_PROXY}" ]; then set -- "$@" -o "Acquire::https::Proxy=${HTTPS_PROXY}"; fi; \
    sed -i 's|http://deb.debian.org|https://deb.debian.org|' /etc/apt/sources.list.d/debian.sources; \
    mv /etc/apt/sources.list.d/pgdg.list /tmp/pgdg.list; \
    apt-get "$@" update --error-on=any; \
    DEBIAN_FRONTEND=noninteractive apt-get "$@" -y upgrade --no-install-recommends; \
    mv /tmp/pgdg.list /etc/apt/sources.list.d/pgdg.list; \
    apt-get clean; rm -rf /var/lib/apt/lists/*; \
    rm /usr/local/bin/gosu

# The server starts as the postgres user instead of starting as root and
# stepping down with gosu, so gosu (a Go binary built with an old Go) is gone.
# The data directory of a new volume is created by the image already owned by
# postgres, and a volume initialised by the root entrypoint is owned by the
# same uid (999).
USER postgres
