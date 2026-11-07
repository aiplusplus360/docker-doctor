# docker-doctor

**Audit a Dockerfile for the problems that turn into an incident: running as root, an unpinned base image, a secret baked into a layer, an apt cache that never refreshes, a shell piped into `sh`, and build tools shipped to production.**

[![CI](https://github.com/aiplusplus360/docker-doctor/actions/workflows/ci.yml/badge.svg)](https://github.com/aiplusplus360/docker-doctor/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)](pyproject.toml)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

```console
$ docker-doctor Dockerfile
Dockerfile:1   high    unpinned-base          base image python has no tag; :latest changes under you
Dockerfile:4   error   runs-as-root           no USER directive; the container runs as uid 0
Dockerfile:9   high    secret-in-arg          ARG API_KEY looks like a credential
Dockerfile:14  medium  apt-lists-not-removed  apt-get install leaves /var/lib/apt/lists in the layer

4 finding(s): 1 error, 2 high, 1 medium
```

---

## Why this exists

A Dockerfile is short enough to review and long enough to hide a problem in. The problems that matter share one property: they are invisible in a running container until something goes wrong.

- **Running as root** works perfectly until a compromised dependency writes to a mounted volume as uid 0.
- **`python:latest`** builds today and builds something else next month. Nobody changed the repo.
- **`ARG API_KEY`** is not a secret store. The value is recorded in image history and readable with `docker history`.
- **`apt-get update` in one layer and `install` in another** uses a stale index forever, because Docker caches the update layer.
- **`curl … | sh`** means the build is not reproducible and the review was meaningless.
- **Build tools left in the final image** are extra attack surface and hundreds of megabytes.

None of these fail a test. All of them are a one-line fix while you are still looking at the Dockerfile.

## Install

No dependencies.

```bash
pip install git+https://github.com/aiplusplus360/docker-doctor
```

## Usage

```bash
docker-doctor Dockerfile                 # lint one file
docker-doctor .                          # find every Dockerfile* under a path
docker-doctor . --format json
docker-doctor . --fail-on high           # CI gate
docker-doctor --explain runs-as-root
docker-doctor --list-rules
```

Exit codes: `0` clean, `1` findings at or above `--fail-on` (default `high`), `2` unreadable input.

## What it checks

| Code | Severity | What it catches |
| --- | --- | --- |
| `runs-as-root` | error | No `USER` in the final stage |
| `unpinned-base` | high | `FROM` without a tag, or with `:latest` |
| `unpinned-base-digest` | low | A tag but no digest pin |
| `secret-in-arg` | high | `ARG`/`ENV` whose name looks like a credential |
| `secret-in-run` | high | Credential-shaped literal inside `RUN` |
| `curl-pipe-shell` | error | `curl`/`wget` piped into `sh`/`bash` |
| `apt-cache-stale` | high | `apt-get install` in a different layer from `apt-get update` |
| `apt-lists-not-removed` | medium | `/var/lib/apt/lists` not cleaned in the same layer |
| `apt-no-install-recommends` | low | `apt-get install` without `--no-install-recommends` |
| `add-instead-of-copy` | medium | `ADD` used for a local path or a tarball |
| `copy-without-chown` | medium | `COPY` into a path while a non-root `USER` is active |
| `build-tools-in-final` | medium | Compilers present in the final stage |
| `sudo-in-build` | high | `sudo` inside `RUN` |
| `no-healthcheck` | low | Final image has no `HEALTHCHECK` |
| `single-stage-build` | info | Build dependencies installed in the runtime stage |

## The part that is not a regex

Parsing a Dockerfile correctly means handling:

- **line continuations** (`\` at end of line) joining a logical instruction;
- **comments** inside a continuation, which Docker allows and which change the line map;
- **heredocs** (`RUN <<EOF … EOF`), which contain arbitrary text that must not be linted as instructions;
- **multi-stage builds**, where "the final image" is a different set of instructions from "the build image", and `USER`/`HEALTHCHECK` only count in the last stage;
- **exec form versus shell form** (`RUN ["a","b"]` is not a shell string);
- **JSON array arguments** (`COPY ["a b", "/dst"]`).

A line-based regex finds `RUN apt-get install` in a comment and misses a real `RUN` split across four lines. This tool builds a stage-aware model first and lints the model.

`build-tools-in-final` depends on that model: `gcc` in the builder stage of a multi-stage build is fine, and the same `gcc` in a single-stage build ships to production.

## Library use

```python
from dockerdoctor import parse_dockerfile, lint

stages = parse_dockerfile(open("Dockerfile").read())
for finding in lint(stages):
    print(finding.severity, finding.code, finding.message)
```

## Tests

```bash
python -m unittest discover -s tests -v
```

The suite covers continuation joining, heredocs, exec-form instructions, stage resolution, and each rule firing on a positive fixture and staying quiet on a negative one.
