"""Closed quality and security execution for Agentic Saga."""

from __future__ import annotations

from typing import Final

import dagger
from dagger import check, dag, function, object_type

PYTHON_IMAGES: Final = (
    (
        "python:3.12.12-bookworm@sha256:"
        "c0abd0758831ad99b7a29e0c1a875da9c4abb9a2e3f21e2eeb585dbcadfb6cd0"
    ),
    (
        "python:3.13.14-bookworm@sha256:"
        "8b9a8b28d9cc221c6ab5d40e9cfcd99429959f6a8f5171612a99147975ab043f"
    ),
)
UV_IMAGE: Final = (
    "ghcr.io/astral-sh/uv:0.11.32@sha256:"
    "df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c"
)
NODE_IMAGE: Final = (
    "node:24.6.0-bookworm-slim@sha256:"
    "9b741b28148b0195d62fa456ed84dd6c953c1f17a3761f3e6e6797a754d9edff"
)
REPOSITORY: Final = "hseshadr/agentic-saga"
REPOSITORY_URL: Final = "https://github.com/hseshadr/agentic-saga.git"
SOURCE_ROOT: Final = "/src"
WEB_ROOT: Final = "/src/web/flight-recorder"
RELEASE_WHEELHOUSE: Final = "/src/dist/release/wheelhouse"
NODE_PATHS: Final = [
    "bin/corepack",
    "bin/node",
    "bin/npm",
    "bin/npx",
    "lib/node_modules/**",
]


async def _guard(source: dagger.Directory, commit_sha: str, git_auth_header: dagger.Secret) -> None:
    guard = dag.foundation().guard(
        source=source,
        repository=REPOSITORY,
        commit_sha=commit_sha,
        http_auth_header=git_auth_header,
    )
    await guard.sync()


async def _release_source(
    source: dagger.Directory, commit_sha: str, git_auth_header: dagger.Secret
) -> dagger.Directory:
    await _guard(source, commit_sha, git_auth_header)
    repository = dag.git(REPOSITORY_URL, http_auth_header=git_auth_header)
    return repository.commit(commit_sha).tree(depth=0, include_tags=True)


async def _dependency_audit(
    source: dagger.Directory, commit_sha: str, git_auth_header: dagger.Secret
) -> None:
    audit = dag.python_package().dependency_audit(
        source=source,
        repository=REPOSITORY,
        commit_sha=commit_sha,
        http_auth_header=git_auth_header,
    )
    await audit.sync()


def _python(source: dagger.Directory, image: str) -> dagger.Container:
    uv = dag.container().from_(UV_IMAGE).file("/uv")
    base = dag.container().from_(image).with_file("/usr/local/bin/uv", uv)
    base = base.with_directory(SOURCE_ROOT, source).with_workdir(SOURCE_ROOT)
    base = base.with_env_variable("UV_PROJECT_ENVIRONMENT", "/opt/venv")
    return base.with_exec(["uv", "sync", "--frozen", "--all-groups", "--all-extras"])


def _node(source: dagger.Directory) -> dagger.Container:
    base = dag.container().from_(NODE_IMAGE).with_directory(SOURCE_ROOT, source)
    return _node_dependencies(base)


def _with_node(base: dagger.Container) -> dagger.Container:
    node = dag.container().from_(NODE_IMAGE).directory("/usr/local")
    return base.with_directory("/usr/local", node, include=NODE_PATHS)


def _node_dependencies(base: dagger.Container) -> dagger.Container:
    result = base.with_workdir(WEB_ROOT).with_env_variable("CI", "1")
    result = result.with_exec(["corepack", "enable"])
    return result.with_exec(["pnpm", "install", "--frozen-lockfile"])


def _with_browsers(base: dagger.Container) -> dagger.Container:
    return base.with_exec(["pnpm", "exec", "playwright", "install", "--with-deps"])


def _frontend(source: dagger.Directory) -> dagger.Container:
    return _with_browsers(_node(source))


def _release(source: dagger.Directory, image: str) -> dagger.Container:
    result = _with_browsers(_node_dependencies(_with_node(_python(source, image))))
    return result.with_workdir(SOURCE_ROOT)


async def _verify_python_matrix(source: dagger.Directory) -> None:
    for image in PYTHON_IMAGES:
        await _python(source, image).with_exec(["uv", "run", "poe", "gate"]).sync()
        candidate = _release(source, image).with_exec(["uv", "run", "poe", "release-candidate"])
        await candidate.sync()
        measured = candidate.with_env_variable(
            "AGENTIC_SAGA_RELEASE_WHEELHOUSE", RELEASE_WHEELHOUSE
        )
        await measured.with_exec(["uv", "run", "python", "scripts/measure_release.py"]).sync()


@object_type
class AgenticSaga:
    """Run Agentic Saga's fixed repository-owned verification commands."""

    @function
    @check
    async def ci(
        self,
        source: dagger.Directory,
        commit_sha: str,
        git_auth_header: dagger.Secret,
    ) -> str:
        """Run guarded dual-runtime, frontend, and measured release gates."""
        verified = await _release_source(source, commit_sha, git_auth_header)
        await _verify_python_matrix(verified)
        await _frontend(verified).with_exec(["pnpm", "gate"]).sync()
        return "Agentic Saga canonical Dagger gate passed"

    @function
    async def security(
        self,
        source: dagger.Directory,
        commit_sha: str,
        git_auth_header: dagger.Secret,
    ) -> str:
        """Run guarded locked Python and frontend dependency audits."""
        await _guard(source, commit_sha, git_auth_header)
        await _dependency_audit(source, commit_sha, git_auth_header)
        await _node(source).with_exec(["pnpm", "audit"]).sync()
        return "Agentic Saga dependency audits passed"
