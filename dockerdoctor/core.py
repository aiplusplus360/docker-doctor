"""Parse a Dockerfile into stages and lint the model, not the text.

A line-based regex misreads a Dockerfile: an instruction can span several
physical lines, a comment can hide inside a continuation, a heredoc contains
arbitrary text that is not an instruction, and only the *final* stage's ``USER``
matters. This module builds a model first.

>>> stages = parse_dockerfile("FROM python:3.12\\nUSER app\\n")
>>> len(stages)
1
>>> main = main_stage(stages)
>>> main.base
'python:3.12'

>>> [f.code for f in lint(parse_dockerfile("FROM python:3.12\\nUSER app\\n"))]
['no-healthcheck', 'unpinned-base-digest']
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

__all__ = [
    "Finding",
    "Instruction",
    "SEVERITIES",
    "Stage",
    "lint",
    "main_stage",
    "parse_dockerfile",
    "severity_at_least",
]

SEVERITIES = ("info", "low", "medium", "high", "error")
_RANK = {name: index for index, name in enumerate(SEVERITIES)}

_CREDENTIAL_WORDS = re.compile(
    r"(SECRET|TOKEN|PASSWORD|PASSWD|APIKEY|API_KEY|PRIVATE_KEY|ACCESS_KEY|"
    r"CREDENTIAL|AUTH|_KEY$|^KEY)",
    re.IGNORECASE,
)
_CREDENTIAL_LITERAL = re.compile(
    r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|access[_-]?key)\b\s*[=:]\s*"
    r"['\"]?([A-Za-z0-9_\-]{12,})['\"]?"
)
_BUILD_TOOLS = {
    "gcc", "g++", "make", "cmake", "build-essential", "golang", "cargo", "rustc",
    "musl-dev", "python3-dev", "libpq-dev", "node-gyp",
}


@dataclass
class Instruction:
    keyword: str
    arguments: str
    line: int
    raw: str
    json_args: Optional[List[str]] = None

    def shell(self) -> str:
        """Return the argument text as a shell string (exec form joined)."""
        if self.json_args is not None:
            return " ".join(self.json_args)
        return self.arguments


@dataclass
class Stage:
    base: str
    alias: Optional[str]
    index: int
    instructions: List[Instruction] = field(default_factory=list)
    line: int = 0

    def named(self, keyword: str) -> List[Instruction]:
        return [i for i in self.instructions if i.keyword == keyword]

    def last(self, keyword: str) -> Optional[Instruction]:
        matches = self.named(keyword)
        return matches[-1] if matches else None


@dataclass
class Finding:
    code: str
    severity: str
    message: str
    hint: str
    file: str
    line: int

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "hint": self.hint,
            "file": self.file,
            "line": self.line,
        }


def severity_at_least(severity: str, threshold: str) -> bool:
    return _RANK[severity] >= _RANK[threshold]


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _strip_comment(line: str) -> str:
    """Remove a full-line comment; Docker has no inline comments."""
    stripped = line.strip()
    if stripped.startswith("#"):
        return ""
    return line


def _join_continuations(text: str) -> List[tuple]:
    """Return ``(logical_line, starting_physical_line)`` pairs."""
    logical: List[tuple] = []
    buffer: List[str] = []
    start = 0

    def flush() -> None:
        nonlocal buffer, start
        if buffer:
            joined = " ".join(part.strip() for part in buffer if part.strip())
            if joined:
                logical.append((joined, start))
            buffer = []

    for number, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw)
        if not line.strip():
            # A comment or blank line inside a continuation is ignored by
            # Docker's parser; the continuation keeps going.
            if buffer:
                continue
            continue
        if not buffer:
            start = number
        if line.rstrip().endswith("\\"):
            buffer.append(line.rstrip()[:-1])
            continue
        buffer.append(line)
        flush()
    flush()
    return logical


def _split_instruction(line: str) -> tuple:
    parts = line.split(None, 1)
    if not parts:
        return "", ""
    keyword = parts[0].upper()
    arguments = parts[1] if len(parts) > 1 else ""
    return keyword, arguments


def _json_array(arguments: str) -> Optional[List[str]]:
    stripped = arguments.strip()
    if not (stripped.startswith("[") and stripped.endswith("]")):
        return None
    # Deliberately not json.loads: Docker allows single quotes in some forms and
    # we only need the elements, not strict JSON semantics.
    inner = stripped[1:-1].strip()
    if not inner:
        return []
    items = re.findall(r'"([^"]*)"', inner) or re.findall(r"'([^']*)'", inner)
    return items if items else None


def parse_dockerfile(text: str, filename: str = "Dockerfile") -> List[Stage]:
    """Parse *text* into build stages, preserving line numbers."""
    stages: List[Stage] = []
    current: Optional[Stage] = None
    lines = _join_continuations(text)
    index = 0

    while index < len(lines):
        logical, line_number = lines[index]
        index += 1
        keyword, arguments = _split_instruction(logical)

        if keyword == "FROM":
            tokens = arguments.split()
            base = tokens[0] if tokens else ""
            alias = None
            for position, token in enumerate(tokens):
                if token.lower() == "as" and position + 1 < len(tokens):
                    alias = tokens[position + 1]
            current = Stage(base=base, alias=alias, index=len(stages), line=line_number)
            stages.append(current)
            continue

        if current is None:
            current = Stage(base="", alias=None, index=0, line=line_number)
            stages.append(current)

        if arguments.strip().startswith("<<"):
            # Heredoc: consume until the terminator so its body is not parsed.
            terminator = arguments.strip()[2:].split()[0].strip("'\"-")
            body: List[str] = []
            while index < len(lines):
                candidate, _ = lines[index]
                index += 1
                if candidate.strip().strip("'\"") == terminator:
                    break
                body.append(candidate)
            instruction = Instruction(
                keyword=keyword,
                arguments=arguments + "\n" + "\n".join(body),
                line=line_number,
                raw=logical,
            )
            current.instructions.append(instruction)
            continue

        current.instructions.append(
            Instruction(
                keyword=keyword,
                arguments=arguments,
                line=line_number,
                raw=logical,
                json_args=_json_array(arguments) if keyword in {"RUN", "CMD",
                                                               "ENTRYPOINT",
                                                               "COPY", "ADD"} else None,
            )
        )

    return stages


def main_stage(stages: Sequence[Stage]) -> Optional[Stage]:
    return stages[-1] if stages else None


def _resolve_base(stage: Stage, stages: Sequence[Stage]) -> str:
    """Resolve ``FROM builder`` to the base image it was built from."""
    for candidate in stages:
        if candidate.alias and candidate.alias == stage.base:
            return candidate.base
    return stage.base


def _image_reference(base: str) -> str:
    return base.split("@", 1)[0]


def _has_tag(reference: str) -> bool:
    if "@" in reference:
        return True
    last_slash = reference.rfind("/")
    return ":" in reference[last_slash + 1:]


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------


def lint(
    stages: Sequence[Stage],
    filename: str = "Dockerfile",
    compose_text: str = "",
) -> List[Finding]:
    findings: List[Finding] = []
    if not stages:
        return findings

    final = main_stage(stages)
    assert final is not None

    def add(code, severity, message, hint, line, file=filename):
        findings.append(Finding(code, severity, message, hint, file, line))

    for stage in stages:
        for instruction in stage.instructions:
            shell = instruction.shell()

            if instruction.keyword in ("ARG", "ENV"):
                for name in _declared_names(instruction):
                    if _CREDENTIAL_WORDS.search(name) and _has_literal(instruction):
                        add(
                            "secret-in-arg",
                            "high",
                            "ARG/ENV %r looks like a credential and carries a literal value"
                            % name,
                            "Build args and env values are stored in image history "
                            "and visible with 'docker history'. Pass secrets at "
                            "runtime, or use BuildKit's --mount=type=secret.",
                            instruction.line,
                        )
                        break

            if instruction.keyword == "RUN":
                if re.search(r"(?i)\b(curl|wget)\b[^|;]*\|\s*(sudo\s+)?(ba)?sh\b", shell):
                    add(
                        "curl-pipe-shell",
                        "error",
                        "a downloaded script is piped straight into a shell",
                        "The build is not reproducible and the review approved a "
                        "different file than the one that runs. Download, verify a "
                        "checksum, then execute.",
                        instruction.line,
                    )
                if re.search(r"(?i)\bsudo\b", shell):
                    add(
                        "sudo-in-build",
                        "high",
                        "RUN uses sudo",
                        "Builds already run as root; sudo is either a no-op that "
                        "signals confusion about the base image, or a sign the "
                        "Dockerfile was copied from a shell script.",
                        instruction.line,
                    )
                match = _CREDENTIAL_LITERAL.search(shell)
                if match and not _looks_like_placeholder(match.group(1)):
                    add(
                        "secret-in-run",
                        "high",
                        "RUN contains a credential-shaped literal",
                        "Anything in a RUN instruction is recorded in the layer. "
                        "Remove the literal and mount the secret at build time.",
                        instruction.line,
                    )
                if re.search(r"(?i)apt-get\s+install", shell):
                    if not re.search(r"(?i)apt-get\s+update", shell):
                        add(
                            "apt-cache-stale",
                            "high",
                            "apt-get install without apt-get update in the same layer",
                            "Docker caches layers. An update in a previous layer is "
                            "not re-run, so the index can be months old and the "
                            "build fails when a package version disappears. Always "
                            "'update && install && rm -rf /var/lib/apt/lists/*' in "
                            "one RUN.",
                            instruction.line,
                        )
                    if not re.search(r"(?i)rm\s+-rf?\s+/var/lib/apt/lists", shell):
                        add(
                            "apt-lists-not-removed",
                            "medium",
                            "apt-get install does not remove /var/lib/apt/lists",
                            "The package index adds tens of megabytes to the layer "
                            "and is never used at runtime. Clean it in the same RUN.",
                            instruction.line,
                        )
                    if "no-install-recommends" not in shell:
                        add(
                            "apt-no-install-recommends",
                            "low",
                            "apt-get install without --no-install-recommends",
                            "Recommended packages are pulled in silently and are "
                            "usually not needed in a server image.",
                            instruction.line,
                        )

            if instruction.keyword == "ADD":
                source = (instruction.json_args or instruction.arguments.split())[0:1]
                first = source[0] if source else ""
                if not first.startswith(("http://", "https://")):
                    add(
                        "add-instead-of-copy",
                        "medium",
                        "ADD is used for a local path",
                        "ADD also extracts archives and fetches URLs, which makes "
                        "the build implicit. Use COPY unless you specifically need "
                        "ADD's magic.",
                        instruction.line,
                    )

            if instruction.keyword == "COPY":
                if "$" not in instruction.arguments and "chown" not in instruction.arguments:
                    if _user_is_not_root(stage, instruction):
                        add(
                            "copy-without-chown",
                            "medium",
                            "COPY runs while a non-root USER is active and does not use --chown",
                            "Files land owned by uid 0 unless --chown is given, and "
                            "the running user cannot write to them.",
                            instruction.line,
                        )

    # Final-stage rules.
    user = final.last("USER")
    if user is None or user.arguments.strip() in ("root", "0", "0:0"):
        add(
            "runs-as-root",
            "error",
            "no USER instruction; the container runs as uid 0",
            "Add a USER in the final stage. A non-root process cannot write outside "
            "its own paths, which turns many container escapes into a permission "
            "error instead of a compromise.",
            final.line,
        )

    if final.last("HEALTHCHECK") is None:
        add(
            "no-healthcheck",
            "low",
            "the final image has no HEALTHCHECK",
            "Orchestrators that do not run external probes cannot distinguish a "
            "hung process from a healthy one.",
            final.line,
        )

    if len(stages) == 1:
        shells = " ".join(i.shell() for i in final.named("RUN"))
        used = sorted(tool for tool in _BUILD_TOOLS if re.search(
            r"(?<![\w-])" + re.escape(tool) + r"(?![\w-])", shells))
        if used:
            add(
                "single-stage-build",
                "info",
                "one build stage installs build tools (%s)" % ", ".join(used[:4]),
                "A multi-stage build keeps compilers out of the runtime image. "
                "Build in a first stage and COPY only the artefacts forward.",
                final.line,
            )
            add(
                "build-tools-in-final",
                "medium",
                "compilers are present in the final image (%s)" % ", ".join(used[:4]),
                "Compilers add hundreds of megabytes and give an attacker a "
                "toolchain. Use a builder stage.",
                final.line,
            )

    # Base image pinning, per stage.
    seen_bases: Dict[str, Stage] = {}
    for stage in stages:
        reference = _image_reference(stage.base)
        if not reference or reference == "scratch":
            continue
        seen_bases.setdefault(reference, stage)
        if reference.lower().startswith(("$", "${")):
            continue
        if not _has_tag(reference):
            add(
                "unpinned-base",
                "high",
                "base image %r has no tag; it resolves to :latest" % reference,
                "':latest' is a moving target. A rebuild can change the base "
                "without a commit. Pin a version, ideally with a digest.",
                stage.line,
            )
        elif reference.endswith(":latest"):
            add(
                "unpinned-base",
                "high",
                "base image %r uses the :latest tag" % reference,
                "Pin a version tag so a rebuild is reproducible.",
                stage.line,
            )
        elif "@" not in stage.base:
            add(
                "unpinned-base-digest",
                "low",
                "base image %r is tagged but not pinned by digest" % reference,
                "A tag can be re-pointed by the registry. A digest cannot. Pin it "
                "in release builds.",
                stage.line,
            )
        if reference.endswith(":latest") and reference not in seen_bases:
            seen_bases[reference] = stage

    if compose_text:
        findings.extend(_lint_compose(compose_text, filename))

    findings.sort(key=lambda f: (-_RANK[f.severity], f.line))
    return findings


def _declared_names(instruction: Instruction) -> List[str]:
    names: List[str] = []
    if instruction.keyword == "ARG":
        for token in instruction.arguments.split():
            if "=" in token:
                names.append(token.split("=", 1)[0])
            elif token.isidentifier():
                names.append(token)
    else:
        for token in re.split(r"\s+", instruction.arguments):
            if "=" in token:
                names.append(token.split("=", 1)[0])
    return names


def _has_literal(instruction: Instruction) -> bool:
    text = instruction.arguments
    for token in text.split():
        if "=" in token:
            value = token.split("=", 1)[1].strip("\"'")
            if value and not value.startswith("$") and not _looks_like_placeholder(value):
                return True
    return False


def _looks_like_placeholder(value: str) -> bool:
    lowered = value.lower()
    if lowered in {"changeme", "password", "secret", "example", "xxx", "test"}:
        return True
    if value.startswith(("$", "${", "<", "%")):
        return True
    if len(set(value)) < 4:
        return True
    return False


def _user_is_not_root(stage: Stage, instruction: Instruction) -> bool:
    for candidate in stage.instructions:
        if candidate.line >= instruction.line:
            break
        if candidate.keyword == "USER":
            return candidate.arguments.strip() not in ("root", "0", "0:0")
    return False


def _lint_compose(text: str, filename: str) -> List[Finding]:
    findings: List[Finding] = []
    for number, line in enumerate(text.splitlines(), 1):
        match = re.match(r"\s*image:\s*(\S+)", line)
        if not match:
            continue
        reference = match.group(1).strip("\"'")
        if reference.endswith(":latest"):
            findings.append(
                Finding(
                    "compose-latest-tag",
                    "high",
                    "service image %r is pinned to :latest" % reference,
                    "Compose will pull a different image on the next up. Pin a "
                    "version or a digest.",
                    filename,
                    number,
                )
            )
    return findings
