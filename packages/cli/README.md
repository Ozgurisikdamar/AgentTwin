# agenttwin CLI

The command line for AgentTwin (spec §39). Its main job is to put a release
gate in CI: `agenttwin release check` does four things:

1. Creates a release of a candidate agent version against its baseline.
2. Waits while AgentTwin simulates the impacted scenarios on both versions and
   compares them.
3. Prints why the gate decided what it decided.
4. Exits with the gate's outcome.

```bash
make cli                       # builds bin/agenttwin
bin/agenttwin --help
```

## Release check

```bash
export AGENTTWIN_URL=https://agenttwin.example.com   # the control plane
export AGENTTWIN_API_KEY=atk_…                       # a project key with the "ci" scope

agenttwin release check \
  --project customer-support \
  --agent support-refund-agent \
  --baseline 1.2.4 \
  --candidate 1.3.0 \
  --commit "$GITHUB_SHA" \
  --ci
```

`--baseline support-refund-agent@1.2.4 --candidate support-refund-agent@1.3.0`
works too. Both versions must already be registered
(`agenttwin agent register manifest.yaml`).

```text
AgentTwin release gate
────────────────────────────────────────────────────────────
Release      support-refund-agent 1.2.4 → 1.3.0 · revision 1
Commit       4e5f6a7b
Gate         BLOCK
             Blocked: duplicate irreversible action (1); success the final
             state disproves (1); …

Required scenarios passed                    6/9   missing: refund-happy-path, …
Irreversible actions in reach tested         1/1

BLOCK  Duplicate irreversible action (duplicate_side_effect)
       An irreversible action must not take effect more than once.
       • refund-timeout-after-mutation (no-double-refund): The side effect refund:ORD-1001 was applied 2 times.
         first divergence: At step 2 the baseline called get_refund_policy(order_id=ORD-1001); the candidate called refund_payment(…).
         trace: 4ad026574fb37d447a7a79f1eecf00ac
…
Risk index   90/100 (sorts releases; never decides)
Evidence     sha256 df7a3425… (verified)
Details      https://agenttwin.example.com/releases/01a0…

Exit code: 3
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | `PASS`, or an override applies (the report says `OVERRIDDEN (originally BLOCK)`) |
| 2 | `WARN`. With `--ci`, only when the project's gate policy fails CI on warnings (`warnFailsCI`); otherwise 0 |
| 3 | `BLOCK`, including missing evidence (`incomplete`) |
| 4 | Infrastructure or configuration error: unreachable URL, bad credential, wrong flag, or a gate that did not decide within `--timeout` |

### Reports

`--format text` (the default) is for people. `--format json` returns the
release, the gate exactly as the API answered it, and the exit code.
`--format junit` produces one test for the gate, one per scenario of the
suite, and one per rule whose evidence names no scenario. A `BLOCK` fails its
tests; a `WARN` fails them only when it fails CI. With `--output FILE`, the
chosen format goes to the file and the text report still goes to the log:

```bash
agenttwin release check … --ci --format junit --output agenttwin-gate.xml
```

### Flags

| Flag | Default | |
|---|---|---|
| `--project` | `$AGENTTWIN_PROJECT` | project id or slug |
| `--agent`, `--baseline`, `--candidate` | | `VERSION` or `AGENT@VERSION` |
| `--commit`, `--base-commit` | `$GITHUB_SHA`, `$CI_COMMIT_SHA`, `$BUILDKITE_COMMIT`, `$GIT_COMMIT` | recorded on the release and its change set |
| `--changed-files FILE` | | changed paths, one per line (`-` reads stdin) |
| `--ci-url` | detected on GitHub Actions, GitLab, Jenkins, Buildkite | the CI run, linked from the release |
| `--title` | | |
| `--release ID` | | check an existing release instead of creating one |
| `--reevaluate` | | start a new evaluation (a new revision) |
| `--no-wait` | | exit 0 once the evaluation is requested |
| `--timeout`, `--poll-interval` | `30m`, `5s` | |
| `--web-url` | `$AGENTTWIN_WEB_URL` | links the report to the web app |

A retried CI job does not create a second release. The CLI sends an
`Idempotency-Key` derived from the request, and the control plane answers
the release it created the first time. It honours this for 24 hours. Pass
`--reevaluate` to evaluate again.

Transient failures (429, 502, 503, 504, connection errors) are retried a
few times with backoff. A wrong URL or a control plane that is down fails
the job in seconds, not at the timeout.

## GitHub Actions

```yaml
- name: Register the candidate
  run: bin/agenttwin agent register --project customer-support --commit "$GITHUB_SHA" agent/manifest.yaml
- name: Release gate
  run: |
    git diff --name-only origin/main... > changed-files.txt
    bin/agenttwin release check --project customer-support \
      --baseline support-refund-agent@1.2.4 --candidate support-refund-agent@1.3.0 \
      --changed-files changed-files.txt --ci --format junit --output agenttwin-gate.xml
  env:
    AGENTTWIN_URL: ${{ vars.AGENTTWIN_URL }}
    AGENTTWIN_API_KEY: ${{ secrets.AGENTTWIN_API_KEY }}
    AGENTTWIN_WEB_URL: ${{ vars.AGENTTWIN_WEB_URL }}
```

## Other commands

* `agenttwin doctor [--project P]` checks that the control plane is ready,
  that the credential is valid, that it holds `release.write`, and that it
  can see the project.
* `agenttwin agent validate FILE` validates a manifest without registering
  it.
* `agenttwin agent register FILE --project P [--commit SHA]` registers a
  version. Registering the same content again is not an error.

## Tests

`go test ./packages/cli/` runs every command against a fake control plane.
The fake answers with the control plane's real responses in `testdata/`,
captured by its integration tests:

```bash
AGENTTWIN_CAPTURE_DIR=$PWD/packages/cli/testdata \
  ./scripts/with-test-infra.sh bash -c 'cd services/control-plane && go test ./internal/server/ -run "TestABadCandidateIsBlocked|TestAnOverride|TestAGoodCandidate|TestReleaseAccessAndValidation"'
```

`gate-pending.json` is the `gate` of `release-created.json`. Every exchange
is held to `control-plane.openapi.yaml`, so the CLI and the API cannot drift
apart unnoticed.
