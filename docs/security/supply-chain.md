# Supply chain

What AgentTwin runs to know what it ships, what it allows, and what was
found and fixed. Every check is a make target that runs the same way on a
laptop and in CI (`.github/workflows/ci.yml`, stage `security`).

| Target | Tool (pinned) | What fails it |
|---|---|---|
| `make secret-scan` | gitleaks v8.30.1 (container), every commit of the history | anything that looks like a credential, outside the exceptions below |
| `make vuln-scan` | govulncheck v1.8.0, pip-audit 2.10.1, `pnpm audit` | a known vulnerability in Go code we call, in any locked Python package, or a high/critical one in the JS workspace |
| `make image-scan` | trivy 0.74.0 (container) over every image of `docker-compose.yml` | the image policy below |
| `make sbom` | trivy 0.74.0 | a document that cannot be written |
| `make supply-chain` | all four | any of them (all run, then it reports) |

`make image-scan` and `make sbom` read local images: `make build` first.
Reports go to `dist/scan/`, CycloneDX SBOMs to `dist/sbom/`; both are
ignored by git and kept by CI as build artifacts. The implementation is
`scripts/supply_chain.py`; the policy itself is tested in
`scripts/tests/test_supply_chain.py`.

Behind a TLS-intercepting proxy the scanner containers use the build proxy
settings of `.env` (`AGENTTWIN_BUILD_HTTPS_PROXY`, `AGENTTWIN_BUILD_CA_BUNDLE`;
infra/docker/compose.build-proxy.yml). Trivy's database is cached in
`~/.cache/agenttwin-trivy` (`AGENTTWIN_TRIVY_CACHE` moves it).

## Image policy

1. **No CRITICAL vulnerability in any image**, fixed upstream or not, unless it
   is a documented exception in [`.trivyignore.yaml`](../../.trivyignore.yaml).
   Each exception carries a statement and an expiry date; after the date the
   finding fails the scan again, so it is looked at again instead of being
   forgotten.
2. **No HIGH or CRITICAL vulnerability that has a fix in an image we build**
   (`agenttwin/*`). A rebuild on the current base picks the fix up; if it
   does not, the Dockerfile or the dependency is changed.
3. **HIGH findings with a fix in third-party images are reported, not failed**
   ("fixed upstream, not yet in this image"): the fix arrives by bumping the
   image, which Dependabot proposes, and a newer image is not automatically
   better (see Grafana below).

Findings without a fix in Debian or Alpine are listed, not failed: there is
nothing to upgrade to yet, and each rebuild picks the fix up when it ships
(our Dockerfiles take the distribution's updates; see "Pinning").

govulncheck fails only on vulnerable code the services actually call. It
still reports vulnerable packages that are imported but not called; the
same module, compiled into an image, then fails the image scan as a fixable
HIGH. Measured: with gRPC put back to v1.83.1, govulncheck reports
GO-2026-6443 as "imported, not called" and passes, while the image scan
fails every Go service image on it.

## Results (2026-09-26)

Counts are distinct vulnerabilities, "fixable / no fix yet".

| Image | Before: CRITICAL | Before: HIGH | After: CRITICAL | After: HIGH | What changed |
|---|---|---|---|---|---|
| agenttwin/control-plane, trace-service, graph-service, runtime-gateway | 0 / 0 | 1 / 0 | 0 / 0 | 0 / 0 | gRPC v1.83.1 → v1.83.2 (GO-2026-6443) |
| agenttwin/web | 0 / 0 | 8 / 0 | 0 / 0 | 0 / 0 | npm, npx, corepack and yarn removed from the runtime image (all 8 were in npm's own dependencies) |
| agenttwin/simulation-service, evaluation-service, demo | 0 / 0 | 0 / 44 | 0 / 0 | 0 / 44 | pip removed from the runtime image (6 fixable medium/low findings); the 44 are Debian packages without a fix |
| agenttwin/postgres (was pgvector/pgvector:0.8.0-pg16) | 8 / 15 | 104 / 78 | 0 / 1 * | 0 / 61 | own image: pgvector 0.8.6 on Debian 13, Debian's updates applied at build (35 packages), gosu removed and the server runs as `postgres` |
| rabbitmq | 2 / 0 | 50 / 0 | 0 / 0 | 0 / 0 | 3.13 → 4.3.6 |
| otel/opentelemetry-collector-contrib | 7 / 0 | 62 / 3 | 0 / 0 | 0 / 0 | 0.135.0 → 0.161.0 |
| prom/prometheus | 2 / 0 | 44 / 2 | 0 / 0 | 0 / 0 | v3.5.0 → v3.15.0 |
| grafana/grafana | 9 / 0 | 88 / 0 | 0 / 0 | 5 / 0 | 12.1.1 → 12.4.11 |
| grafana/k6 (load test only) | 4 / 0 | 63 / 0 | 0 / 0 | 2 / 0 | 1.3.0 → 2.3.0 |

\* CVE-2026-6653, the documented exception below.

Dependencies: govulncheck, pip-audit (every locked Python package, with
hashes) and `pnpm audit` report nothing. Secrets: nothing in the 116 commits of the history
beyond the exceptions below.

What the numbers left open, and why it is acceptable:

* **Debian packages without a fix** (Python images: util-linux, ncurses,
  systemd libraries, perl-base; PostgreSQL image: the same plus gnupg,
  libxml2, libacl). None of the services runs these programs; they come
  with the base image. The images are rebuilt from the current base, so a
  Debian fix arrives with the next build.
* **Grafana 12.4.11**: 5 HIGH with fixes (thrift and tempo compiled into the
  Grafana binary, OpenSSL in Alpine). Grafana 13.2.2 is not the answer: it
  has 104 HIGH, in the data source plugins it bundles. Grafana listens on
  127.0.0.1 only and holds dashboards, not tenant data.
* **k6 2.3.0**: OpenSSL in Alpine (fixed in 3.5.8-r0). k6 runs only during
  `make load-test`, on the compose network.
* **tzdata in distroless** (Go images): an "UNKNOWN" advisory for new time
  zone data, not a vulnerability; the services use UTC.

### Exception

| Id | Where | Why it does not apply | Expires |
|---|---|---|---|
| CVE-2026-6653 (CRITICAL) | libxml2 in agenttwin/postgres | Debian has no fixed package yet. PostgreSQL uses libxml2 only for the `xml` type and XML functions, which no AgentTwin service uses (checked: no XML in any SQL of the repository), and the database is not reachable from outside the compose network. The Dockerfile applies Debian's updates at build time, so the fix is picked up when it ships. | 2026-12-31 |

Proof that the expiry works: with `expired_at` set in the past,
`make image-scan` fails `agenttwin/postgres:dev` on CVE-2026-6653 (exit 2).

## Secret scanning

gitleaks' default rules, over every commit, with two narrow allowlists in
[`.gitleaks.toml`](../../.gitleaks.toml) and two fingerprints in
[`.gitleaksignore`](../../.gitleaksignore). Without them the default rules
flag 321 places in the history. Each was looked at; none is a credential:

| Allowlisted by | Findings | What they are |
|---|---|---|
| path `apps/web/tests/unit/*-fixtures.ts` | 225 | API responses captured from the services for the UI tests: hashes, ids |
| path `testdata/` | 64 | captured responses and CLI golden files |
| path `services/evaluation-service/tests/data/` | 3 | evaluation fixtures |
| path `packages/contracts/fixtures/redaction.json` | 2 | inputs the redaction tests must remove |
| value `refund-ORD-<n>` | 14 | idempotency keys of the demo refunds |
| value containing `0123456789` | 7 | the fake secrets the tests use |
| value `${VAR}` | 1 | an environment reference in compose |
| match `apikey:<uuid>` | 2 | an API key's *id*, used as the actor of an audit entry |
| match `idempotency_key=` | 1 | an idempotency key |
| fingerprint (`.gitleaksignore`) | 2 | a redaction canary string; a made-up value the config loader must accept |

Proof that it catches a real one: a GitHub token committed in a scratch
clone is reported (`github-pat`, value redacted, exit code 3). gitleaks
exits 1 both when it finds something and when it cannot run (a missing
config); the scan asks for exit code 3 on findings, so a broken run is not
mistaken for either result.

## Pinning

* **Lockfiles**: `go.sum`, `uv.lock`, `pnpm-lock.yaml`; builds and CI install
  with `--frozen` / `--frozen-lockfile`.
* **Images we build**: base images pinned to a major/minor line
  (`golang:1.25-alpine`, `python:3.12-slim`, `node:22-alpine`,
  `pgvector/pgvector:0.8.6-pg16-trixie`, distroless `static-debian12:nonroot`).
  The line is the contract; the patch level floats so that a rebuild takes
  the line's security fixes. What a given image contains is recorded by its
  SBOM.
* **Third-party images**: exact versions in `docker-compose.yml`.
* **Tools**: uv (`infra/docker/python.Dockerfile`), the scanners (above), the
  GitHub Actions (by commit SHA, in `.github/workflows/`).
* **Updates**: Dependabot (`.github/dependabot.yml`) proposes updates for Go
  modules, Python (uv), npm, the Dockerfiles, the compose images and the
  Actions; a proposal goes through the same CI, supply-chain stage included.

## Upgrading a stack that already has data

This round changed the database and broker images under existing volumes.
Both were tried on a volume created by the previous images.

* **PostgreSQL** (Debian 12 → 13, PostgreSQL 16.10 → 16.15, pgvector 0.8.0 →
  0.8.6): the server starts on the old volume as the `postgres` user. glibc
  changed (2.36 → 2.41), so PostgreSQL warns about a collation version
  mismatch on every connection until text indexes are rebuilt; `make doctor`
  says so. `make db-upgrade` (scripts/postgres-upgrade.sh) reindexes the
  affected databases, records the new collation version and updates the
  pgvector extension; run it once after the upgrade (it locks each table
  while it reindexes it). It does nothing on a current volume.
* **RabbitMQ** (3.13 → 4.3.6): a 3.13 node with its feature flags enabled
  (the default) upgrades in place. RabbitMQ names its node, and its data
  directory, after the container's hostname, which Docker sets to the
  container id: until now every recreated container (a new image, a changed
  setting) started as a new, empty node, and anything still queued stayed
  behind in the volume under the old name. The broker now has a fixed
  hostname (`rabbitmq`), and a dead letter survives a recreate (checked).
  The first start with the fixed name is itself such a change: drain the
  queues (`make doctor` shows their depth) before upgrading a stack that
  holds messages you need.
