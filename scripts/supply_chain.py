"""Supply-chain checks (docs/security/supply-chain.md).

    make secret-scan    # gitleaks over the whole git history
    make vuln-scan      # govulncheck, pip-audit and pnpm audit over the lockfiles
    make image-scan     # trivy over every image the stack runs (make build first)
    make sbom           # CycloneDX SBOMs of our images and of the source tree
    make supply-chain   # all four; every check runs, then it fails if any failed

The scanners are pinned below and run in containers (trivy, gitleaks) or
through the toolchains the repository already needs (go, uv, pnpm), so the
same commands run on a laptop and in CI. Reports go to ``dist/scan`` and
SBOMs to ``dist/sbom`` (both ignored by git; CI keeps them as artifacts).

Image policy:

* no CRITICAL vulnerability in any image, fixed upstream or not, unless it
  is a documented exception in ``.trivyignore.yaml`` (with a statement and
  an expiry date, after which it fails again);
* no HIGH or CRITICAL vulnerability that has a fix in an image we build
  (``agenttwin/*``): a rebuild on the current base picks the fix up;
* HIGH findings with a fix in third-party images are reported, not failed:
  they are fixed by bumping the image, which the report lists.

Behind a TLS-intercepting proxy, the scanners use the same settings as the
image builds (``AGENTTWIN_BUILD_HTTPS_PROXY``, ``AGENTTWIN_BUILD_CA_BUNDLE``
in ``.env``, see infra/docker/compose.build-proxy.yml).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIR = ROOT / "dist" / "scan"
SBOM_DIR = ROOT / "dist" / "sbom"
IGNORE_FILE = ".trivyignore.yaml"

TRIVY_IMAGE = "ghcr.io/aquasecurity/trivy:0.74.0"
GITLEAKS_IMAGE = "ghcr.io/gitleaks/gitleaks:v8.30.1"
GOVULNCHECK = "golang.org/x/vuln/cmd/govulncheck@v1.8.0"
PIP_AUDIT = "pip-audit==2.10.1"

OURS = "agenttwin/"
LEAKS_FOUND = 3  # gitleaks' exit code when it found something
# Directories trivy skips when it inventories the source tree: installed
# copies of what the lockfiles already list, and build output.
SOURCE_SKIP = ("node_modules", ".venv", ".next", "dist", "bin", ".git")


# ---------------------------------------------------------------- policy


@dataclass(frozen=True, order=True)
class Finding:
    severity: str
    vulnerability: str
    package: str
    installed: str
    fixed: str
    target: str


@dataclass
class ImageVerdict:
    image: str
    ours: bool
    findings: list[Finding]
    violations: list[Finding] = field(default_factory=list)
    suppressed: list[tuple[str, str]] = field(default_factory=list)

    def count(self, severity: str, *, fixable: bool) -> int:
        return sum(1 for f in self.findings if f.severity == severity and bool(f.fixed) == fixable)


def findings(report: dict[str, Any]) -> list[Finding]:
    """The vulnerabilities of a trivy JSON report, one per package and id."""
    out: set[Finding] = set()
    for result in report.get("Results") or []:
        for v in result.get("Vulnerabilities") or []:
            out.add(
                Finding(
                    severity=v.get("Severity", "UNKNOWN"),
                    vulnerability=v["VulnerabilityID"],
                    package=v.get("PkgName", ""),
                    installed=v.get("InstalledVersion", ""),
                    fixed=v.get("FixedVersion", "") or "",
                    target=result.get("Target", ""),
                )
            )
    return sorted(out)


def suppressed(report: dict[str, Any]) -> list[tuple[str, str]]:
    """(vulnerability id, statement) of the findings the ignore file removed."""
    out: set[tuple[str, str]] = set()
    for result in report.get("Results") or []:
        for m in result.get("ExperimentalModifiedFindings") or []:
            finding = m.get("Finding") or {}
            vid = finding.get("VulnerabilityID")
            if vid:
                out.add((vid, m.get("Statement", "")))
    return sorted(out)


def violations(image: str, found: Iterable[Finding]) -> list[Finding]:
    """What the image policy (module docstring) does not allow."""
    ours = image.startswith(OURS)
    return [f for f in found if f.severity == "CRITICAL" or (ours and f.severity == "HIGH" and f.fixed)]


def verdict(image: str, report: dict[str, Any]) -> ImageVerdict:
    found = findings(report)
    return ImageVerdict(
        image=image,
        ours=image.startswith(OURS),
        findings=found,
        violations=violations(image, found),
        suppressed=suppressed(report),
    )


# ---------------------------------------------------------------- running


def log(msg: str) -> None:
    print(msg, flush=True)


def run(cmd: Sequence[str], *, shown: Sequence[str] = (), **kw: Any) -> int:
    """Runs a command and returns its exit code; logs ``shown`` (default: the command)."""
    log("$ " + " ".join(shown or cmd))
    return subprocess.run(cmd, check=False, **kw).returncode  # noqa: S603 - fixed tools, repo paths


def container_net() -> list[str]:
    """docker run flags for the outbound proxy, if the build uses one."""
    flags: list[str] = []
    proxy = os.environ.get("AGENTTWIN_BUILD_HTTPS_PROXY", "")
    if proxy:
        flags += ["--network", "host", "-e", f"HTTPS_PROXY={proxy}", "-e", f"HTTP_PROXY={proxy}"]
        no_proxy = os.environ.get("AGENTTWIN_BUILD_NO_PROXY", "")
        if no_proxy:
            flags += ["-e", f"NO_PROXY={no_proxy}"]
    ca = os.environ.get("AGENTTWIN_BUILD_CA_BUNDLE", "")
    if ca:
        inside = "/etc/ssl/certs/scan-ca.crt"
        flags += ["-v", f"{ca}:{inside}:ro", "-e", f"SSL_CERT_FILE={inside}"]
    return flags


def trivy(args: Sequence[str]) -> int:
    cache = Path(os.environ.get("AGENTTWIN_TRIVY_CACHE", Path.home() / ".cache" / "agenttwin-trivy"))
    cache.mkdir(parents=True, exist_ok=True)
    SCAN_DIR.mkdir(parents=True, exist_ok=True)
    SBOM_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        "docker", "run", "--rm", *container_net(),
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        "-v", f"{cache}:/root/.cache/trivy",
        "-v", f"{ROOT}:/repo:ro",
        "-v", f"{SCAN_DIR}:/out/scan",
        "-v", f"{SBOM_DIR}:/out/sbom",
        TRIVY_IMAGE, *args,
    ]  # fmt: skip
    return run(cmd, shown=["trivy", *args])


def compose_images() -> list[str]:
    """Every image the compose files run, the load test's k6 included."""
    out = subprocess.run(
        ["docker", "compose", "--profile", "load", "config", "--images"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted({line.strip() for line in out.splitlines() if line.strip()})


def file_name(image: str) -> str:
    return image.replace("/", "_").replace(":", "_")


def secrets() -> bool:
    """gitleaks over every commit (CI checks out the full history)."""
    SCAN_DIR.mkdir(parents=True, exist_ok=True)
    args = [
        "git", "/repo",
        "--config", "/repo/.gitleaks.toml",
        "--gitleaks-ignore-path", "/repo/.gitleaksignore",
        "--redact", "--no-banner", "--log-level", "warn", "--exit-code", str(LEAKS_FOUND),
        "--report-format", "json", "--report-path", "/out/gitleaks.json",
    ]  # fmt: skip
    mounts = ["-v", f"{ROOT}:/repo:ro", "-v", f"{SCAN_DIR}:/out"]
    rc = run(["docker", "run", "--rm", *mounts, GITLEAKS_IMAGE, *args], shown=["gitleaks", *args])
    # gitleaks also exits 1 when it cannot run (a missing config, a bad
    # repository): only its own exit code for findings means findings.
    if rc not in (0, LEAKS_FOUND):
        log(f"secrets: gitleaks failed ({rc})")
        return False
    leaks = json.loads((SCAN_DIR / "gitleaks.json").read_text() or "[]")
    for leak in leaks:
        where = f"{leak.get('File')}:{leak.get('StartLine')}"
        log(f"  secret? {leak.get('RuleID')} in {where} (commit {leak.get('Commit', '')[:10]})")
    log(f"secrets: {len(leaks)} findings")
    return not leaks


def python_requirements(dest: Path, *, hashes: bool) -> bool:
    """The locked environment of every workspace member as a requirements file.

    Neither pip-audit nor trivy reads a uv workspace lockfile with several
    root packages (trivy: "uv lockfile must contain 1 root package")."""
    export = [
        "uv", "export", "--quiet", "--frozen", "--all-packages", "--all-extras", "--all-groups",
        "--no-emit-workspace", "--format", "requirements-txt", "-o", str(dest),
    ]  # fmt: skip
    return run([*export] if hashes else [*export, "--no-hashes"], cwd=ROOT) == 0


def deps() -> bool:
    """Known vulnerabilities in the Go, Python and JavaScript dependencies."""
    ok = True
    # Go: only vulnerabilities in code the modules actually call fail it.
    ok &= run(["go", "run", GOVULNCHECK, "./..."], cwd=ROOT) == 0
    # Python: every pinned package, checked against its hash.
    with tempfile.TemporaryDirectory() as tmp:
        reqs = Path(tmp) / "requirements.txt"
        ok &= python_requirements(reqs, hashes=True)
        audit = ["uvx", "--from", PIP_AUDIT, "pip-audit", "--disable-pip", "--progress-spinner", "off"]
        ok &= run([*audit, "-r", str(reqs)], cwd=ROOT) == 0
    # JavaScript: the whole pnpm workspace, development tools included.
    ok &= run(["pnpm", "audit", "--audit-level", "high"], cwd=ROOT) == 0
    log(f"dependencies: {'no known vulnerabilities' if ok else 'FAILED (see above)'}")
    return ok


def images() -> bool:
    """trivy over every image of the stack, judged by the image policy."""
    verdicts: list[ImageVerdict] = []
    for image in compose_images():
        name = file_name(image)
        rc = trivy([
            "image", "--quiet", "--scanners", "vuln", "--format", "json", "--show-suppressed",
            "--ignorefile", f"/repo/{IGNORE_FILE}", "--output", f"/out/scan/{name}.json", image,
        ])  # fmt: skip
        if rc != 0:
            log(f"  {image}: trivy failed ({rc}) - is it built (make build) or pullable?")
            not_scanned = Finding("ERROR", "not-scanned", "", "", "", image)
            verdicts.append(ImageVerdict(image, image.startswith(OURS), [], [not_scanned]))
            continue
        verdicts.append(verdict(image, json.loads((SCAN_DIR / f"{name}.json").read_text())))
    return report(verdicts)


def report(verdicts: list[ImageVerdict]) -> bool:
    log("")
    log(f"{'image':52} {'CRITICAL':>12} {'HIGH':>12}  verdict")
    log(f"{'':52} {'fix/no fix':>12} {'fix/no fix':>12}")
    for v in verdicts:
        crit = f"{v.count('CRITICAL', fixable=True)}/{v.count('CRITICAL', fixable=False)}"
        high = f"{v.count('HIGH', fixable=True)}/{v.count('HIGH', fixable=False)}"
        state = "ok"
        if v.violations:
            state = "FAIL"
        elif not v.ours and v.count("HIGH", fixable=True):
            state = "ok (HIGH fixed upstream, not yet in this image)"
        log(f"{v.image:52} {crit:>12} {high:>12}  {state}")
    for v in verdicts:
        for f in v.violations:
            fix = f"fixed in {f.fixed}" if f.fixed else "no fix yet"
            log(f"  FAIL {v.image}: {f.severity} {f.vulnerability} {f.package} {f.installed} ({fix})")
        for vid, statement in v.suppressed:
            log(f"  exception {v.image}: {vid} - {statement}")
    failed = [v.image for v in verdicts if v.violations]
    log(f"images: {len(verdicts)} scanned, {len(failed)} failed the policy")
    return not failed


def sbom() -> bool:
    """CycloneDX SBOMs: each image we build, the Go and JavaScript lockfiles
    of the source tree, and the locked Python environment."""
    ok = True
    for image in compose_images():
        if image.startswith(OURS):
            out = f"/out/sbom/{file_name(image)}.cdx.json"
            ok &= trivy(["image", "--quiet", "--format", "cyclonedx", "--output", out, image]) == 0
    skip = [a for d in SOURCE_SKIP for a in ("--skip-dirs", f"**/{d}")]
    out = "/out/sbom/source.cdx.json"
    skip += ["--skip-files", "uv.lock"]  # covered by python.cdx.json
    ok &= trivy(["fs", "--quiet", "--format", "cyclonedx", *skip, "--output", out, "/repo"]) == 0
    python = SCAN_DIR / "python"
    python.mkdir(parents=True, exist_ok=True)
    ok &= python_requirements(python / "requirements.txt", hashes=False)
    out = "/out/sbom/python.cdx.json"
    ok &= trivy(["fs", "--quiet", "--format", "cyclonedx", "--output", out, "/out/scan/python"]) == 0
    written = sorted(p.name for p in SBOM_DIR.glob("*.cdx.json"))
    log(f"sbom: {len(written)} documents in {SBOM_DIR.relative_to(ROOT)}: {', '.join(written)}")
    return ok


CHECKS = {"secrets": secrets, "deps": deps, "images": images, "sbom": sbom}


def main(argv: Sequence[str] | None = None) -> int:
    formatter = argparse.RawDescriptionHelpFormatter
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=formatter)
    parser.add_argument("check", choices=[*CHECKS, "all"])
    args = parser.parse_args(argv)
    names = list(CHECKS) if args.check == "all" else [args.check]
    failed = [name for name in names if not CHECKS[name]()]
    if failed:
        log(f"supply chain: FAILED: {', '.join(failed)}")
        return 1
    log("supply chain: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
