"""Closed quality and security execution for Agentic Saga."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
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
RELEASE_ROOT: Final = "/src/dist/release"
FRONTEND_COVERAGE: Final = "/src/web/flight-recorder/coverage"
WEB_NODE_MODULES: Final = "/src/web/flight-recorder/node_modules"
BROWSER_CACHE: Final = "/root/.cache/ms-playwright"
QUALITY_PROOF: Final = "/src/dist/release/quality-proof.json"
PYTHON_LOCK_INPUTS: Final = ("pyproject.toml", "uv.lock")
FRONTEND_LOCK_INPUTS: Final = (
    "web/flight-recorder/package.json",
    "web/flight-recorder/pnpm-lock.yaml",
)
RUNTIME_WHEELHOUSES: Final = (
    "/src/dist/release/wheelhouses/3.12",
    "/src/dist/release/wheelhouses/3.13",
)
NODE_PATHS: Final = (
    "bin/corepack",
    "bin/node",
    "bin/npm",
    "bin/npx",
    "lib/node_modules/**",
)


@dataclass(frozen=True)
class FrontendArtifacts:
    node: dagger.Directory
    packages: dagger.Directory
    browsers: dagger.Directory
    coverage: dagger.Directory


async def _run_bounded(operation: Awaitable[object], semaphore: asyncio.Semaphore) -> None:
    async with semaphore:
        await operation


async def _bounded_gather(*operations: Awaitable[object], limit: int = 2) -> None:
    semaphore = asyncio.Semaphore(limit)
    await asyncio.gather(*(_run_bounded(operation, semaphore) for operation in operations))


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


def _source_layer(
    base: dagger.Container, source: dagger.Directory, paths: tuple[str, ...]
) -> dagger.Container:
    return base.with_directory(SOURCE_ROOT, source, include=list(paths))


def _python_dependencies(source: dagger.Directory, image: str) -> dagger.Container:
    uv = dag.container().from_(UV_IMAGE).file("/uv")
    base = dag.container().from_(image).with_file("/usr/local/bin/uv", uv)
    base = _source_layer(base, source, PYTHON_LOCK_INPUTS).with_workdir(SOURCE_ROOT)
    base = base.with_env_variable("UV_PROJECT_ENVIRONMENT", "/opt/venv")
    return base.with_exec(
        ["uv", "sync", "--frozen", "--all-groups", "--all-extras", "--no-install-project"]
    )


def _install_project(base: dagger.Container, source: dagger.Directory) -> dagger.Container:
    complete = base.with_directory(SOURCE_ROOT, source).with_workdir(SOURCE_ROOT)
    return complete.with_exec(
        ["uv", "sync", "--frozen", "--all-groups", "--all-extras", "--offline"]
    )


def _python(source: dagger.Directory, image: str) -> dagger.Container:
    return _install_project(_python_dependencies(source, image), source)


def _node_packages(base: dagger.Container, source: dagger.Directory) -> dagger.Container:
    locked = _source_layer(base, source, FRONTEND_LOCK_INPUTS)
    locked = locked.with_workdir(WEB_ROOT).with_env_variable("CI", "1")
    locked = locked.with_exec(["corepack", "enable"])
    return locked.with_exec(["pnpm", "install", "--frozen-lockfile"])


def _with_browsers(base: dagger.Container) -> dagger.Container:
    return base.with_exec(["pnpm", "exec", "playwright", "install", "--with-deps", "chromium"])


def _node(source: dagger.Directory) -> dagger.Container:
    base = _node_packages(dag.container().from_(NODE_IMAGE), source)
    return base.with_directory(SOURCE_ROOT, source).with_workdir(WEB_ROOT)


def _frontend_dependencies(source: dagger.Directory) -> dagger.Container:
    return _with_browsers(_node_packages(dag.container().from_(NODE_IMAGE), source))


def _mount_frontend(base: dagger.Container, artifacts: FrontendArtifacts) -> dagger.Container:
    base = base.with_directory("/usr/local", artifacts.node, include=list(NODE_PATHS))
    base = base.with_directory(WEB_NODE_MODULES, artifacts.packages)
    return base.with_directory(BROWSER_CACHE, artifacts.browsers).with_workdir(WEB_ROOT)


def _release(source: dagger.Directory, image: str, frontend: FrontendArtifacts) -> dagger.Container:
    base = _mount_frontend(_python_dependencies(source, image), frontend)
    base = base.with_exec(["pnpm", "exec", "playwright", "install-deps", "chromium"])
    return _install_project(base, source)


def _artifact_builder(source: dagger.Directory) -> dagger.Container:
    return _python(source, PYTHON_IMAGES[-1]).with_exec(["uv", "run", "poe", "artifacts"])


def _frontend_builder(source: dagger.Directory, dependencies: dagger.Container) -> dagger.Container:
    complete = dependencies.with_directory(SOURCE_ROOT, source).with_workdir(WEB_ROOT)
    return complete.with_exec(["pnpm", "gate"])


def _frontend_artifacts(
    dependencies: dagger.Container, proof: dagger.Container
) -> FrontendArtifacts:
    return FrontendArtifacts(
        dependencies.directory("/usr/local"),
        dependencies.directory(WEB_NODE_MODULES),
        dependencies.directory(BROWSER_CACHE),
        proof.directory(FRONTEND_COVERAGE),
    )


def _quality_proof_command() -> list[str]:
    statement = (
        "from pathlib import Path; from scripts.quality_proof import write_quality_proof; "
        f"write_quality_proof(Path('{QUALITY_PROOF}'))"
    )
    return ["uv", "run", "python", "-c", statement]


def _measurement_command() -> list[str]:
    return [
        "uv",
        "run",
        "python",
        "scripts/measure_release.py",
        "--quality-proof",
        QUALITY_PROOF,
    ]


def _proved_candidate(
    source: dagger.Directory,
    image: str,
    artifacts: dagger.Directory,
    frontend: FrontendArtifacts,
) -> dagger.Container:
    gated = _release(source, image, frontend).with_exec(["uv", "run", "poe", "gate"])
    candidate = gated.with_directory(RELEASE_ROOT, artifacts)
    candidate = candidate.with_exec(["uv", "run", "poe", "release-candidate"])
    proved = candidate.with_directory(FRONTEND_COVERAGE, frontend.coverage)
    return proved.with_exec(_quality_proof_command())


async def _runtime_lane(
    source: dagger.Directory,
    image: str,
    wheelhouse: str,
    artifacts: dagger.Directory,
    frontend: FrontendArtifacts,
) -> None:
    proved = _proved_candidate(source, image, artifacts, frontend)
    measured = proved.with_env_variable("AGENTIC_SAGA_RELEASE_WHEELHOUSE", wheelhouse)
    await measured.with_exec(_measurement_command()).sync()


async def _shared_outputs(
    source: dagger.Directory,
) -> tuple[dagger.Directory, FrontendArtifacts]:
    artifact_builder = _artifact_builder(source)
    dependencies = _frontend_dependencies(source)
    frontend_builder = _frontend_builder(source, dependencies)
    await _bounded_gather(artifact_builder.sync(), frontend_builder.sync())
    artifacts = artifact_builder.directory(RELEASE_ROOT)
    return artifacts, _frontend_artifacts(dependencies, frontend_builder)


async def _runtime_matrix(
    source: dagger.Directory, artifacts: dagger.Directory, frontend: FrontendArtifacts
) -> None:
    lanes = zip(PYTHON_IMAGES, RUNTIME_WHEELHOUSES, strict=True)
    operations = (_runtime_lane(source, *lane, artifacts, frontend) for lane in lanes)
    await _bounded_gather(*operations, limit=2)


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
        artifacts, frontend = await _shared_outputs(verified)
        await _runtime_matrix(verified, artifacts, frontend)
        return "Agentic Saga canonical Dagger gate passed"

    @function
    async def security(
        self,
        source: dagger.Directory,
        commit_sha: str,
        git_auth_header: dagger.Secret,
    ) -> str:
        """Run guarded locked Python and frontend dependency audits."""
        await _dependency_audit(source, commit_sha, git_auth_header)
        await _node(source).with_exec(["pnpm", "audit"]).sync()
        return "Agentic Saga dependency audits passed"
