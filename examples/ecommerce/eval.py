from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from collections.abc import Sequence
from importlib import import_module
from pathlib import Path
from typing import Protocol, cast

from examples.ecommerce.evaluation import CorpusValidationError, EvalSuite, load_corpus
from examples.ecommerce.live_eval import (
    LiveEvalConfigurationError,
    LiveEvalOptions,
    LiveEvalReportArtifact,
    run_live_corpus,
)


class _LoadDotenv(Protocol):
    def __call__(self, dotenv_path: Path, *, override: bool) -> bool: ...


_CORPUS = Path(__file__).with_name("eval-corpus-v1.json")
_OUTPUT = Path(".artifacts/eval")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate 24 transaction fixtures or run opt-in model-decision checks."
    )
    parser.add_argument("--live", action="store_true", help="allow paid OpenRouter calls")
    parser.add_argument("--corpus", type=Path, default=_CORPUS)
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument(
        "--suite", type=EvalSuite, choices=tuple(EvalSuite), default=EvalSuite.SMOKE
    )
    parser.add_argument("--output", type=Path, default=_OUTPUT)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if not arguments.live:
        return _validate(arguments.corpus, arguments.json)
    error = _prepare_live_environment()
    if error is not None:
        print(error, file=sys.stderr)
        return 2
    return _run_live(arguments)


def _prepare_live_environment() -> str | None:
    error = _load_dotenv()
    if error is not None:
        return error
    if os.environ.get("RUN_LIVE_MODEL_EVALS") != "1":
        return "Set RUN_LIVE_MODEL_EVALS=1 to authorize paid model evaluation."
    if not os.environ.get("OPENROUTER_API_KEY"):
        return "Set OPENROUTER_API_KEY to run the live model evaluation."
    return None


def _load_dotenv() -> str | None:
    path = Path.cwd() / ".env"
    if not path.is_file():
        return None
    try:
        module = import_module("dotenv")
    except ModuleNotFoundError:
        return "Install the agent extra to load the local .env file."
    loader = cast(_LoadDotenv, module.load_dotenv)
    loader(path, override=False)
    return None


def _validate(path: Path, json_output: bool) -> int:
    try:
        cases = load_corpus(path)
    except CorpusValidationError:
        print("Corpus validation failed.", file=sys.stderr)
        return 2
    counts = Counter(case.category.value for case in cases)
    summary = {"case_count": len(cases), "categories": dict(sorted(counts.items()))}
    _emit_value(summary, json_output)
    return 0


def _run_live(arguments: argparse.Namespace) -> int:
    try:
        report = asyncio.run(
            run_live_corpus(
                arguments.corpus,
                arguments.samples,
                arguments.output,
                LiveEvalOptions(suite=arguments.suite),
            )
        )
    except (CorpusValidationError, LiveEvalConfigurationError, OSError):
        print("Live evaluation failed safely; inspect configuration.", file=sys.stderr)
        return 2
    _emit_report(report, arguments.json)
    return 0 if report.score.thresholds_met else 1


def _emit_value(value: object, json_output: bool) -> None:
    if json_output:
        print(json.dumps(value, sort_keys=True, separators=(",", ":")))
        return
    summary = cast(dict[str, object], value)
    print(f"Validated {summary['case_count']} transaction fixtures; no network call was made.")


def _emit_report(report: LiveEvalReportArtifact, json_output: bool) -> None:
    if json_output:
        _emit_value(report.model_dump(mode="json"), True)
        return
    print(f"Model/provider: {report.identity.configured_model} via {report.identity.provider}")
    for sample in report.samples:
        usage = _usage_text(sample.input_tokens, sample.output_tokens, sample.cost_usd)
        print(
            f"{sample.case_id}: tool={sample.selected_tool or 'none'} "
            f"correct={sample.decision_correct} latency_ms={sample.latency_ms} {usage}"
        )
    print(f"Model decision accuracy: {report.score.decision_accuracy}")
    print("Transaction correctness: evaluated separately by Temporal integration tests.")


def _usage_text(input_tokens: int | None, output_tokens: int | None, cost: object) -> str:
    if input_tokens is None and output_tokens is None and cost is None:
        return "usage=unavailable"
    return f"input_tokens={input_tokens} output_tokens={output_tokens} cost_usd={cost}"


if __name__ == "__main__":
    raise SystemExit(main())
