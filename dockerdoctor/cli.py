"""Command line interface for docker-doctor."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Sequence

from .core import SEVERITIES, Finding, lint, main_stage, parse_dockerfile, severity_at_least

__version__ = "0.1.0"

_RULES = {
    "runs-as-root": "The final stage has no USER, so every process in the container is uid 0.",
    "unpinned-base": "FROM without a tag, or with :latest, is not reproducible.",
    "secret-in-arg": "ARG/ENV values are recorded in image history and visible in `docker history`.",
    "curl-pipe-shell": "Piping a download into sh makes the build unreviewable.",
    "apt-cache-stale": "apt-get update in an earlier layer is cached and never re-runs.",
    "apt-lists-not-removed": "The package index is dead weight in every layer that installs.",
    "add-instead-of-copy": "ADD extracts archives and fetches URLs implicitly.",
    "copy-without-chown": "COPY writes as uid 0 while a non-root USER is active.",
    "build-tools-in-final": "Compilers in the runtime image are attack surface.",
    "single-stage-build": "A builder stage keeps toolchains out of production.",
    "no-healthcheck": "Without HEALTHCHECK a hung process looks healthy.",
    "sudo-in-build": "Builds already run as root; sudo signals a copied shell script.",
    "secret-in-run": "RUN content is recorded in the layer.",
    "apt-no-install-recommends": "Recommended packages are pulled in silently.",
    "compose-latest-tag": "Compose will pull a different image on the next up.",
    "unpinned-base-digest": "A tag can be re-pointed; a digest cannot.",
}


def _find_dockerfiles(paths: Sequence[str]) -> List[str]:
    found: List[str] = []
    for path in paths:
        if os.path.isdir(path):
            for base, dirs, names in os.walk(path):
                dirs[:] = [d for d in dirs if d not in {
                    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}]
                for name in sorted(names):
                    lowered = name.lower()
                    if lowered == "dockerfile" or lowered.startswith("dockerfile.") \
                            or lowered.endswith(".dockerfile"):
                        found.append(os.path.join(base, name))
        else:
            found.append(path)
    return found


def _format_table(findings: Sequence[Finding]) -> str:
    if not findings:
        return "no findings"
    lines = ["%-30s %-7s %-26s %s" % ("LOCATION", "SEV", "RULE", "FINDING")]
    lines.append("-" * 100)
    for finding in findings:
        location = "%s:%d" % (os.path.basename(finding.file), finding.line)
        lines.append("%-30s %-7s %-26s %s" % (
            location, finding.severity, finding.code, finding.message))
        for wrapped in _wrap(finding.hint, 84):
            lines.append(" " * 38 + wrapped)
    counts = {}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    summary = ", ".join(
        "%d %s" % (counts[s], s) for s in reversed(SEVERITIES) if s in counts
    )
    lines.append("-" * 100)
    lines.append("%d finding(s): %s" % (len(findings), summary))
    return "\n".join(lines)


def _wrap(text: str, width: int) -> List[str]:
    words = text.split()
    lines, current = [], ""
    for word in words:
        if len(current) + len(word) + 1 > width and current:
            lines.append(current)
            current = word
        else:
            current = (current + " " + word).strip()
    if current:
        lines.append(current)
    return lines


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docker-doctor", description="Audit Dockerfiles for production hazards.")
    parser.add_argument("paths", nargs="*", default=["."])
    parser.add_argument("--format", choices=("table", "json"), default="table")
    parser.add_argument("--fail-on", choices=SEVERITIES, default="high")
    parser.add_argument("--explain", metavar="CODE")
    parser.add_argument("--list-rules", action="store_true")
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_rules:
        for code in sorted(_RULES):
            print("%-28s %s" % (code, _RULES[code]))
        return 0

    if args.explain:
        note = _RULES.get(args.explain)
        if note is None:
            sys.stderr.write("unknown rule: %s\n" % args.explain)
            return 2
        print(note)
        return 0

    files = _find_dockerfiles(args.paths or ["."])
    if not files:
        sys.stderr.write("docker-doctor: no Dockerfile found\n")
        return 2

    findings: List[Finding] = []
    for path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError as exc:
            sys.stderr.write("docker-doctor: %s\n" % exc)
            return 2
        compose = ""
        for sibling in ("docker-compose.yml", "docker-compose.yaml", "compose.yml"):
            candidate = os.path.join(os.path.dirname(path) or ".", sibling)
            if os.path.isfile(candidate):
                with open(candidate, "r", encoding="utf-8", errors="replace") as handle:
                    compose = handle.read()
                break
        findings.extend(lint(parse_dockerfile(text, path), path, compose))

    if args.format == "json":
        print(json.dumps({"findings": [f.as_dict() for f in findings],
                          "count": len(findings)}, indent=2))
    else:
        print(_format_table(findings))

    return 1 if any(severity_at_least(f.severity, args.fail_on) for f in findings) else 0
