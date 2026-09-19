import hashlib
import io
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Mapping
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
from zipfile import ZipFile

import pytest

ROOT = Path(__file__).parents[1]
BUILD_SCRIPT = ROOT / "scripts" / "build_release_artifacts.sh"
RUNTIME_WHEELHOUSE_SCRIPT = ROOT / "scripts" / "build_runtime_wheelhouse.sh"
VERIFY_SCRIPT = ROOT / "scripts" / "verify_release_candidate.sh"
PACKAGE_STATIC = ROOT / "src" / "agentic_saga" / "demo" / "static"
SOURCE_TRACES = ROOT / "examples" / "ecommerce" / "flight-recorder" / "traces"
EXPECTED_TRACES = {
    "business-failure.json",
    "compensation-failure.json",
    "happy-path.json",
    "index.json",
    "lost-response.json",
}
REQUIRED_SDIST_DOCUMENTS = {
    "CHANGELOG.md",
    "docs/architecture/agentic-saga.architecture.json",
    "docs/architecture/index.html",
    "docs/flight-recorder.md",
    "docs/operations.md",
}
_MARKDOWN_INLINE_LINK = re.compile(r"!?\[[^\]]*\]\(\s*(?:<([^>]+)>|([^\s)]+))")
_MARKDOWN_REFERENCE_LINK = re.compile(r"^\s*\[[^\]]+\]:\s*(?:<([^>]+)>|([^\s]+))", re.MULTILINE)
_RUNTIME_EXPORT_COMMAND = (
    "uv",
    "export",
    "--locked",
    "--offline",
    "--no-dev",
    "--no-emit-project",
    "--format",
    "requirements.txt",
)


class _HtmlLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.targets: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del tag
        self.targets.extend(
            value for name, value in attrs if name in {"href", "src"} and value is not None
        )


def _script_environment(overrides: Mapping[str, str] | None) -> dict[str, str]:
    environment = dict(os.environ)
    if overrides is not None:
        environment.update(overrides)
    return environment


def _script_command(script: Path, output: str | Path) -> list[str]:
    return [str(script), str(output)]


def _run_process(
    command: list[str], cwd: Path, env: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=env, capture_output=True, check=False, text=True)


def _run_script(
    script: Path,
    output: str | Path,
    *,
    cwd: Path = ROOT,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return _run_process(_script_command(script, output), cwd, _script_environment(env))


def _dist_output(tmp_path: Path, label: str) -> Path:
    return ROOT / "dist" / f"release-contract-{label}-{tmp_path.name}"


def _head_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, check=True, text=True
    )
    return result.stdout.strip()


def _remove_path(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _single_artifacts(output: Path) -> tuple[Path, Path]:
    wheels = list(output.glob("*.whl"))
    sdists = list(output.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise AssertionError("expected exactly one wheel and one sdist")
    return wheels[0], sdists[0]


def _safe_sdist_files(path: Path) -> dict[str, bytes]:
    with tarfile.open(path) as archive:
        members = archive.getmembers()
        root = _validate_sdist_members(members)
        files: dict[str, bytes] = {}
        for member in members:
            relative = PurePosixPath(member.name).relative_to(root).as_posix()
            if member.isdir() or relative == ".":
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                raise AssertionError(f"sdist regular file cannot be read: {member.name}")
            files[relative] = extracted.read()
    return files


def _validate_sdist_members(members: list[tarfile.TarInfo]) -> str:
    roots: set[str] = set()
    names: set[str] = set()
    for member in members:
        path = _validated_sdist_path(member)
        if member.name in names:
            raise AssertionError(f"duplicate sdist member: {member.name}")
        names.add(member.name)
        roots.add(path.parts[0])
    return _single_sdist_root(roots)


def _validated_sdist_path(member: tarfile.TarInfo) -> PurePosixPath:
    path = PurePosixPath(member.name)
    unsafe = not member.name or "\\" in member.name or path.is_absolute()
    if unsafe or any(part in {".", ".."} for part in path.parts):
        raise AssertionError(f"unsafe sdist member path: {member.name}")
    if not member.isdir() and not member.isfile():
        raise AssertionError(f"unsafe sdist member type: {member.name}")
    return path


def _single_sdist_root(roots: set[str]) -> str:
    if len(roots) != 1:
        raise AssertionError("sdist must contain exactly one top-level directory")
    return roots.pop()


def _sdist_names(path: Path) -> set[str]:
    return set(_safe_sdist_files(path))


def _linked_targets(name: str, content: bytes) -> set[str]:
    text = content.decode("utf-8")
    targets = {
        first or second
        for pattern in (_MARKDOWN_INLINE_LINK, _MARKDOWN_REFERENCE_LINK)
        for first, second in pattern.findall(text)
    }
    if name.endswith(".html"):
        parser = _HtmlLinkParser()
        parser.feed(text)
        targets.update(parser.targets)
    return targets


def _assert_relative_links_resolve(files: Mapping[str, bytes]) -> None:
    names = set(files)
    for name, content in files.items():
        if not name.endswith((".md", ".html")):
            continue
        for target in _linked_targets(name, content):
            url = urlsplit(target)
            if url.scheme or url.netloc or not url.path or url.path.startswith("/"):
                continue
            resolved = posixpath.normpath(
                posixpath.join(PurePosixPath(name).parent.as_posix(), unquote(url.path))
            )
            exists = resolved in names or any(
                candidate.startswith(f"{resolved}/") for candidate in names
            )
            assert exists, f"sdist link target is missing: {name} -> {target}"


def _write_test_sdist(path: Path, members: list[tuple[str, bytes, bytes]]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, member_type, content in members:
            member = tarfile.TarInfo(name)
            member.type = member_type
            member.size = len(content) if member_type == tarfile.REGTYPE else 0
            if member_type in {tarfile.SYMTYPE, tarfile.LNKTYPE}:
                member.linkname = "outside"
            archive.addfile(member, io.BytesIO(content) if member_type == tarfile.REGTYPE else None)


def _direct_build(output: Path) -> tuple[Path, Path]:
    command = [
        "uv",
        "build",
        "--offline",
        "--no-python-downloads",
        "--no-build-isolation",
        "--out-dir",
        str(output),
    ]
    result = _run_process(command, ROOT, os.environ)
    assert result.returncode == 0, result.stderr
    return _single_artifacts(output)


def _build_working_tree_candidate(output: Path) -> None:
    """Isolate installed-wheel checks from the separately tested exact-commit builder."""
    output.mkdir(parents=True)
    requirements = output / "runtime-requirements.txt"
    _export_runtime_requirements(requirements)
    wheel, sdist = _direct_build(output)
    (output / "SOURCE_COMMIT").write_text(f"{_head_commit()}\n")
    _write_candidate_manifest(output, (wheel, sdist, requirements))
    _build_runtime_wheelhouse(output)


def _export_runtime_requirements(requirements: Path) -> None:
    command = [*_RUNTIME_EXPORT_COMMAND, "-o", str(requirements)]
    result = _run_process(command, ROOT, os.environ)
    assert result.returncode == 0, result.stderr


def _write_candidate_manifest(output: Path, artifacts: tuple[Path, ...]) -> None:
    rows = (f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in artifacts)
    (output / "SHA256SUMS").write_text("".join(rows))


def _package_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _archive_group(archive: ZipFile, prefix: str) -> dict[str, bytes]:
    return {
        name.removeprefix(prefix): archive.read(name)
        for name in archive.namelist()
        if name.startswith(prefix) and not name.endswith("/")
    }


def _build(output: Path) -> None:
    result = _run_script(BUILD_SCRIPT, output)
    assert result.returncode == 0, result.stderr


def _build_runtime_wheelhouse(output: Path) -> None:
    result = _run_script(RUNTIME_WHEELHOUSE_SCRIPT, output)
    assert result.returncode == 0, result.stderr


def _artifact_digests(output: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in output.iterdir()
        if path.is_file()
    }


def _release_path_links(output: Path, tmp_path: Path) -> tuple[Path, Path]:
    direct = ROOT / "dist" / f"release-contract-direct-link-{tmp_path.name}"
    ancestor = ROOT / "dist" / f"release-contract-ancestor-link-{tmp_path.name}"
    _remove_path(direct)
    _remove_path(ancestor)
    direct.symlink_to(output, target_is_directory=True)
    ancestor.symlink_to(output.parent, target_is_directory=True)
    return direct, ancestor / output.name


def _build_blocking_uv(tmp_path: Path) -> Path:
    real_uv = shutil.which("uv")
    assert real_uv is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "uv"
    wrapper.write_text(f'#!/bin/sh\n[ "$1" != "build" ] || exit 97\nexec "{real_uv}" "$@"\n')
    wrapper.chmod(0o755)
    return bin_dir


def _prepare_escape_link(tmp_path: Path) -> tuple[Path, Path]:
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("must survive")
    link = ROOT / "dist" / f"release-contract-link-{tmp_path.name}"
    link.parent.mkdir(parents=True, exist_ok=True)
    _remove_path(link)
    link.symlink_to(outside, target_is_directory=True)
    return link, marker


def _assert_artifacts(output: Path) -> None:
    _build(output)
    wheel, sdist = _single_artifacts(output)
    with ZipFile(wheel) as archive:
        assert any(name.endswith("entry_points.txt") for name in archive.namelist())
    required = {
        ".env.example",
        "LICENSE",
        "SECURITY.md",
        "PROVENANCE.md",
        "THIRD_PARTY_NOTICES.md",
        "docs/agent-adapter.md",
    }
    assert required <= _sdist_names(sdist)
    requirements = (output / "runtime-requirements.txt").read_text()
    assert "pydantic==" in requirements
    assert "--hash=sha256:" in requirements
    assert not (output / "wheelhouse").exists()
    assert not (output / "wheelhouses").exists()


def _assert_committed_identity(output: Path, dirty_file: Path) -> None:
    _build(output)
    _, sdist = _single_artifacts(output)
    assert (output / "SOURCE_COMMIT").read_text().strip() == _head_commit()
    assert (output / "SHA256SUMS").is_file()
    with tarfile.open(sdist) as archive:
        names = {member.name.split("/", 1)[-1] for member in archive.getmembers()}
    assert dirty_file.name not in names


def test_release_scripts_are_strict_and_non_publishing() -> None:
    scripts = [
        ROOT / "scripts" / "build_release_artifacts.sh",
        ROOT / "scripts" / "build_runtime_wheelhouse.sh",
        ROOT / "scripts" / "verify_release_candidate.sh",
    ]
    for script in scripts:
        content = script.read_text()
        assert "set -euo pipefail" in content
        assert "twine upload" not in content
        assert "uv publish" not in content


def test_build_configuration_includes_trust_files() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert '"/.env.example"' in pyproject
    assert '"/LICENSE"' in pyproject
    assert '"/SECURITY.md"' in pyproject
    assert '"/PROVENANCE.md"' in pyproject
    assert '"/THIRD_PARTY_NOTICES.md"' in pyproject
    assert '"/CHANGELOG.md"' in pyproject
    assert '"/docs/operations.md"' in pyproject
    assert '"/docs/architecture"' in pyproject
    assert '"/docs/flight-recorder.md"' in pyproject


def test_wheel_contains_exact_reviewed_static_and_reference_bytes(tmp_path: Path) -> None:
    wheel, _ = _direct_build(tmp_path / "dist")

    with ZipFile(wheel) as archive:
        static = _archive_group(archive, "agentic_saga/demo/static/")
        traces = _archive_group(archive, "agentic_saga/demo/traces/")

    assert static == _package_files(PACKAGE_STATIC)
    assert traces == _package_files(SOURCE_TRACES)
    assert set(traces) == EXPECTED_TRACES


def test_wheel_carries_bundled_browser_dependency_notices(tmp_path: Path) -> None:
    wheel, _ = _direct_build(tmp_path / "dist")

    with ZipFile(wheel) as archive:
        notices = [name for name in archive.namelist() if name.endswith("/THIRD_PARTY_NOTICES.md")]
        assert len(notices) == 1
        notice = archive.read(notices[0]).decode()

    assert "react-dom 19.2.8" in notice
    assert "Permission is hereby granted, free of charge" in notice


def test_wheel_marks_agentic_saga_as_a_typed_package(tmp_path: Path) -> None:
    # Given
    wheel, _ = _direct_build(tmp_path / "dist")

    # When
    with ZipFile(wheel) as archive:
        names = frozenset(archive.namelist())

    # Then
    assert "agentic_saga/py.typed" in names


def test_sdist_contains_exact_required_documents_and_resolvable_relative_links(
    tmp_path: Path,
) -> None:
    _, sdist = _direct_build(tmp_path / "dist")
    files = _safe_sdist_files(sdist)

    for name in REQUIRED_SDIST_DOCUMENTS:
        assert files[name] == (ROOT / name).read_bytes()
    _assert_relative_links_resolve(files)


@pytest.mark.parametrize(
    "member_type",
    [
        tarfile.SYMTYPE,
        tarfile.LNKTYPE,
        tarfile.CHRTYPE,
        tarfile.BLKTYPE,
        tarfile.FIFOTYPE,
        b"Z",
    ],
)
def test_sdist_validation_rejects_unsafe_member_types(tmp_path: Path, member_type: bytes) -> None:
    sdist = tmp_path / "unsafe-type.tar.gz"
    _write_test_sdist(
        sdist,
        [
            ("agentic_saga-0.1.0/README.md", tarfile.REGTYPE, b"safe"),
            ("agentic_saga-0.1.0/escape", member_type, b""),
        ],
    )

    with pytest.raises(AssertionError, match="unsafe sdist member type"):
        _safe_sdist_files(sdist)


@pytest.mark.parametrize(
    "unsafe_name",
    [
        "/absolute/escape",
        "../escape",
        "agentic_saga-0.1.0/../escape",
        "agentic_saga-0.1.0\\escape",
        "other-root/escape",
    ],
)
def test_sdist_validation_rejects_path_escapes(tmp_path: Path, unsafe_name: str) -> None:
    sdist = tmp_path / "unsafe-path.tar.gz"
    _write_test_sdist(
        sdist,
        [
            ("agentic_saga-0.1.0/README.md", tarfile.REGTYPE, b"safe"),
            (unsafe_name, tarfile.REGTYPE, b"escape"),
        ],
    )

    with pytest.raises(AssertionError, match=r"unsafe sdist member path|top-level directory"):
        _safe_sdist_files(sdist)


def test_sdist_validation_rejects_duplicate_members(tmp_path: Path) -> None:
    sdist = tmp_path / "duplicate.tar.gz"
    duplicate = ("agentic_saga-0.1.0/README.md", tarfile.REGTYPE, b"same")
    _write_test_sdist(sdist, [duplicate, duplicate])

    with pytest.raises(AssertionError, match="duplicate sdist member"):
        _safe_sdist_files(sdist)


def test_sdist_validation_rejects_missing_relative_link_target() -> None:
    files = {"docs/guide.md": b"Read the [missing contract](missing.md)."}

    with pytest.raises(AssertionError, match=r"docs/guide\.md -> missing\.md"):
        _assert_relative_links_resolve(files)


def test_build_rejects_output_outside_dist_without_deleting_target(tmp_path: Path) -> None:
    output = tmp_path / "outside"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("must survive")

    result = _run_script(BUILD_SCRIPT, output)

    assert result.returncode != 0
    assert marker.read_text() == "must survive"


def test_build_rejects_dot_without_deleting_working_directory(tmp_path: Path) -> None:
    working_directory = tmp_path / "working"
    working_directory.mkdir()
    marker = working_directory / "keep.txt"
    marker.write_text("must survive")

    result = _run_script(BUILD_SCRIPT, ".", cwd=working_directory)

    assert result.returncode != 0
    assert marker.read_text() == "must survive"


def test_build_rejects_repository_root_without_deleting_it(tmp_path: Path) -> None:
    isolated_root = tmp_path / "isolated-repository"
    scripts = isolated_root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(BUILD_SCRIPT, scripts / BUILD_SCRIPT.name)
    marker = isolated_root / "keep.txt"
    marker.write_text("must survive")

    result = _run_script(scripts / BUILD_SCRIPT.name, isolated_root)

    assert result.returncode != 0
    assert marker.read_text() == "must survive"


def test_build_rejects_filesystem_root_before_running_cleanup(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_rm = fake_bin / "rm"
    fake_rm.write_text("#!/bin/sh\nexit 97\n")
    fake_rm.chmod(0o755)

    result = _run_script(BUILD_SCRIPT, "/", env={"PATH": f"{fake_bin}:{os.environ['PATH']}"})

    assert result.returncode != 0
    assert "unsafe" in result.stderr.lower()


def test_build_rejects_symlink_escape_without_deleting_target(tmp_path: Path) -> None:
    link, marker = _prepare_escape_link(tmp_path)

    try:
        result = _run_script(BUILD_SCRIPT, link)
        assert result.returncode != 0
        assert marker.read_text() == "must survive"
        assert link.is_symlink()
    finally:
        _remove_path(link)


def test_build_produces_one_wheel_and_sdist_with_trust_files(tmp_path: Path) -> None:
    output = _dist_output(tmp_path, "artifacts")
    try:
        _assert_artifacts(output)
    finally:
        shutil.rmtree(output, ignore_errors=True)


def test_build_uses_committed_head_and_excludes_dirty_files(tmp_path: Path) -> None:
    output = _dist_output(tmp_path, "identity")
    dirty_file = ROOT / "dirty-release-sentinel.txt"
    dirty_file.write_text("must not enter committed archive")
    try:
        _assert_committed_identity(output, dirty_file)
    finally:
        dirty_file.unlink(missing_ok=True)
        shutil.rmtree(output, ignore_errors=True)


def test_should_preserve_first_party_artifacts_when_building_a_second_runtime(
    tmp_path: Path,
) -> None:
    # Given a complete first-party release artifact set.
    output = _dist_output(tmp_path, "runtime-wheelhouse")
    try:
        _build(output)
        before = _artifact_digests(output)

        # When the executing runtime downloads its locked dependency wheels.
        _build_runtime_wheelhouse(output)

        # Then the release artifacts remain byte-for-byte identical and versioned.
        assert _artifact_digests(output) == before
        version = f"{sys.version_info.major}.{sys.version_info.minor}"
        assert sorted(path.name for path in (output / "wheelhouses").iterdir()) == [version]
    finally:
        shutil.rmtree(output, ignore_errors=True)


def test_should_reject_modified_build_once_artifact_when_verifying_candidate(
    tmp_path: Path,
) -> None:
    # Given a valid first-party release artifact set with a modified wheel.
    output = _dist_output(tmp_path, "build-once-tamper")
    try:
        _build(output)
        wheel, _ = _single_artifacts(output)
        wheel.write_bytes(b"tampered")

        # When the release candidate is verified.
        result = _run_script(VERIFY_SCRIPT, output)

        # Then verification fails before it can install or rebuild the wheel.
        assert result.returncode == 1
        assert "artifact digest mismatch" in result.stderr
    finally:
        shutil.rmtree(output, ignore_errors=True)


def test_should_reject_tampered_requirements_before_candidate_install(tmp_path: Path) -> None:
    # Given a valid first-party artifact set with modified locked requirements.
    output = _dist_output(tmp_path, "requirements-candidate-tamper")
    try:
        _build(output)
        (output / "runtime-requirements.txt").write_text("tampered\n")

        # When candidate verification runs.
        result = _run_script(VERIFY_SCRIPT, output)

        # Then it fails before creating a wheelhouse or installing dependencies.
        assert result.returncode == 1
        assert "artifact digest mismatch: runtime-requirements.txt" in result.stderr
        assert not (output / "wheelhouses").exists()
    finally:
        shutil.rmtree(output, ignore_errors=True)


def test_should_reject_tampered_requirements_before_runtime_download(tmp_path: Path) -> None:
    # Given a valid first-party artifact set with modified locked requirements.
    output = _dist_output(tmp_path, "requirements-runtime-tamper")
    try:
        _build(output)
        (output / "runtime-requirements.txt").write_text("tampered\n")

        # When the runtime wheelhouse builder runs directly.
        result = _run_script(RUNTIME_WHEELHOUSE_SCRIPT, output)

        # Then it refuses before invoking pip download.
        assert result.returncode == 1
        assert "artifact digest mismatch: runtime-requirements.txt" in result.stderr
        assert not (output / "wheelhouses").exists()
    finally:
        shutil.rmtree(output, ignore_errors=True)


def test_should_reject_runtime_wheelhouse_when_source_commit_is_wrong(tmp_path: Path) -> None:
    # Given a first-party artifact set whose source identity no longer matches HEAD.
    output = _dist_output(tmp_path, "runtime-wrong-commit")
    try:
        _build(output)
        (output / "SOURCE_COMMIT").write_text("0" * 40 + "\n")

        # When the standalone wheelhouse builder validates the artifact set.
        result = _run_script(RUNTIME_WHEELHOUSE_SCRIPT, output)

        # Then it rejects the mismatch before downloading dependencies.
        assert result.returncode == 1
        assert "commit does not match HEAD" in result.stderr
        assert not (output / "wheelhouses").exists()
    finally:
        shutil.rmtree(output, ignore_errors=True)


def test_should_reject_direct_and_ancestor_symlink_release_paths(tmp_path: Path) -> None:
    # Given direct and ancestor symlinks that both resolve inside dist.
    output = _dist_output(tmp_path, "symlink-paths")
    try:
        _build(output)
        direct, nested = _release_path_links(output, tmp_path)

        # When either release script receives either symlinked path.
        for script in (BUILD_SCRIPT, RUNTIME_WHEELHOUSE_SCRIPT, VERIFY_SCRIPT):
            for unsafe_path in (direct, nested):
                result = _run_script(script, unsafe_path)

                # Then it fails closed without traversing the symlink path.
                assert result.returncode == 2
                assert "unsafe" in result.stderr
    finally:
        _remove_path(ROOT / "dist" / f"release-contract-direct-link-{tmp_path.name}")
        _remove_path(ROOT / "dist" / f"release-contract-ancestor-link-{tmp_path.name}")
        shutil.rmtree(output, ignore_errors=True)


def test_should_reuse_valid_artifacts_without_invoking_uv_build(tmp_path: Path) -> None:
    # Given a complete valid artifact set and a uv wrapper that blocks builds.
    output = _dist_output(tmp_path, "reuse-without-build")
    try:
        _build_working_tree_candidate(output)
        blocking_bin = _build_blocking_uv(tmp_path)

        # When candidate verification receives the existing artifact set.
        result = _run_script(
            VERIFY_SCRIPT,
            output,
            env={"PATH": f"{blocking_bin}:{os.environ['PATH']}", "UV_OFFLINE": "1"},
        )

        # Then verification succeeds because it never calls uv build.
        assert result.returncode == 0, result.stderr
    finally:
        shutil.rmtree(output, ignore_errors=True)


def test_verify_accepts_intact_working_tree_artifacts_offline(tmp_path: Path) -> None:
    output = _dist_output(tmp_path, "intact")
    try:
        _build_working_tree_candidate(output)
        result = _run_script(
            VERIFY_SCRIPT,
            output,
            env={"UV_OFFLINE": "1", "UV_PYTHON_DOWNLOADS": "never"},
        )
        assert result.returncode == 0, result.stderr
    finally:
        shutil.rmtree(output, ignore_errors=True)


def test_verify_rejects_tampered_artifact_without_rebuilding(tmp_path: Path) -> None:
    output = _dist_output(tmp_path, "artifact-tamper")
    try:
        _build(output)
        wheel, _ = _single_artifacts(output)
        wheel.write_bytes(wheel.read_bytes() + b"tampered")
        result = _run_script(VERIFY_SCRIPT, output)
        assert result.returncode != 0
        assert "digest" in result.stderr.lower()
    finally:
        shutil.rmtree(output, ignore_errors=True)


def test_verify_rejects_tampered_manifest_without_rebuilding(tmp_path: Path) -> None:
    output = _dist_output(tmp_path, "manifest-tamper")
    try:
        _build(output)
        manifest = output / "SHA256SUMS"
        lines = manifest.read_text().splitlines()
        manifest.write_text("0" * 64 + lines[0][64:] + "\n" + "\n".join(lines[1:]) + "\n")
        result = _run_script(VERIFY_SCRIPT, output)
        assert result.returncode != 0
        assert "digest" in result.stderr.lower()
    finally:
        shutil.rmtree(output, ignore_errors=True)


def test_verify_rejects_mismatched_recorded_commit_without_rebuilding(tmp_path: Path) -> None:
    output = _dist_output(tmp_path, "commit-tamper")
    try:
        _build(output)
        (output / "SOURCE_COMMIT").write_text("0" * 40 + "\n")
        result = _run_script(VERIFY_SCRIPT, output)
        assert result.returncode != 0
        assert "commit" in result.stderr.lower()
    finally:
        shutil.rmtree(output, ignore_errors=True)


def test_verify_installs_cli_with_network_disabled(tmp_path: Path) -> None:
    output = _dist_output(tmp_path, "verify")
    try:
        _build_working_tree_candidate(output)
        result = _run_script(
            VERIFY_SCRIPT,
            output,
            env={"UV_OFFLINE": "1", "UV_PYTHON_DOWNLOADS": "never"},
        )

        assert result.returncode == 0, result.stderr
    finally:
        shutil.rmtree(output, ignore_errors=True)


def test_release_proof_requires_frozen_hashed_offline_inputs() -> None:
    build = BUILD_SCRIPT.read_text()
    verify = VERIFY_SCRIPT.read_text()

    assert "uv export" in build
    assert "--locked" in build
    assert "--offline" in build
    assert "--no-python-downloads" in build
    assert "--require-hashes" in verify
    assert "--offline" in verify
    assert "--no-python-downloads" in verify
    assert "runtime-requirements.txt" in verify
    assert "pip download" not in build
    assert "pip download" in RUNTIME_WHEELHOUSE_SCRIPT.read_text()
    assert "--only-binary=:all:" in RUNTIME_WHEELHOUSE_SCRIPT.read_text()
    assert '--find-links "$wheelhouse"' in verify
    assert '--python "$release_python"' in verify
    assert "--python 3.13" not in verify
