"""Event text never reaches dagger-for-github's bash script as a `${{ }}` expression.

dagger-for-github pastes every `with:` input except `module` into a bash script. An
expression there is rendered before bash runs, so any value an event author can shape
becomes shell text. hseshadr/ci's fleet rule `dagger-args-expression` rejects it. Values
arrive through `env:` instead, and the args only test or quote the variable.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import cast

import pytest
import yaml

WORKFLOWS = Path(__file__).parents[2] / ".github" / "workflows"
DAGGER_ACTION = "dagger/dagger-for-github@"
#: Expression roots whose values an event author, dispatcher, or fork controls.
ATTACKER_EXPRESSIONS = ("${{ inputs.", "${{ github.event.", "${{ github.head_ref")
#: The action passes `module` as INPUT_MODULE; every other input is pasted into bash.
ENV_ONLY_INPUTS = frozenset({"module"})
AUTH_ARGUMENT = "--git-auth-header=env:DAGGER_GIT_HTTP_AUTH_HEADER"
COMMIT_SHA = "a" * 40
#: USE_GIT_AUTH values a hostile or broken environment could carry.
HOSTILE_SWITCHES = (
    "true",
    "false",
    "$(touch pwned)",
    "`touch pwned`",
    "x;touch pwned",
    "x --source=/",
    'x" ; touch pwned ; "',
    "x\ntouch pwned",
)

Step = dict[str, object]


def _steps(name: str) -> list[Step]:
    workflow = cast(dict[str, object], yaml.safe_load((WORKFLOWS / name).read_text()))
    jobs = cast(dict[str, dict[str, object]], workflow["jobs"])
    return [step for job in jobs.values() for step in cast(list[Step], job["steps"])]


def _dagger_step(name: str) -> Step:
    steps = [step for step in _steps(name) if str(step.get("uses", "")).startswith(DAGGER_ACTION)]
    assert len(steps) == 1
    return steps[0]


def _script_inputs() -> list[tuple[str, str, str]]:
    return [
        (path.name, key, str(value))
        for path in sorted(WORKFLOWS.glob("*.yml"))
        for step in _steps(path.name)
        if str(step.get("uses", "")).startswith(DAGGER_ACTION)
        for key, value in cast(Step, step.get("with", {})).items()
        if key not in ENV_ONLY_INPUTS
    ]


def _expand_like_the_action(args: str, switch: str, cwd: Path) -> list[str]:
    """Render `${{ github.sha }}` as Actions does, then let bash expand the args."""
    bash = shutil.which("bash")
    assert bash is not None
    rendered = args.replace("${{ github.sha }}", COMMIT_SHA)
    result = subprocess.run(  # noqa: S603 - fixed bash, test-owned argv
        [bash, "-c", f"printf '%s\\0' {rendered}"],
        env={"USE_GIT_AUTH": switch, "PATH": "/usr/bin:/bin"},
        cwd=cwd,
        capture_output=True,
        check=True,
    )
    return result.stdout.decode().split("\0")[:-1]


def test_should_paste_no_attacker_controlled_expression_into_any_dagger_input() -> None:
    # Given every input dagger-for-github pastes into its bash script.
    inputs = _script_inputs()

    # When each one is searched for an event-controlled expression.
    pasted = [
        (name, key, text)
        for name, key, text in inputs
        if any(expression in text for expression in ATTACKER_EXPRESSIONS)
    ]

    # Then none is found.
    assert inputs
    assert pasted == []


@pytest.mark.parametrize(
    ("name", "verb"), (("dagger.yml", "ci"), ("dagger-security.yml", "security"))
)
def test_should_omit_git_auth_when_the_switch_is_empty(
    name: str, verb: str, tmp_path: Path
) -> None:
    # Given the real args of a public or untrusted event, where USE_GIT_AUTH renders empty.
    args = str(cast(Step, _dagger_step(name)["with"])["args"])

    # When bash expands them.
    argv = _expand_like_the_action(args, "", tmp_path)

    # Then Dagger receives no auth argument at all.
    assert argv == [verb, f"--commit-sha={COMMIT_SHA}"]


@pytest.mark.parametrize("switch", HOSTILE_SWITCHES)
@pytest.mark.parametrize(
    ("name", "verb"), (("dagger.yml", "ci"), ("dagger-security.yml", "security"))
)
def test_should_turn_any_non_empty_switch_into_only_the_fixed_auth_argument(
    name: str, verb: str, switch: str, tmp_path: Path
) -> None:
    # Given the real args and any non-empty USE_GIT_AUTH value.
    args = str(cast(Step, _dagger_step(name)["with"])["args"])

    # When bash expands them.
    argv = _expand_like_the_action(args, switch, tmp_path)

    # Then the value is only a presence test: Dagger gets the fixed literal and bash ran nothing.
    assert argv == [verb, f"--commit-sha={COMMIT_SHA}", AUTH_ARGUMENT]
    assert not (tmp_path / "pwned").exists()
