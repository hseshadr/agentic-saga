import { useEffect, useState } from "react";
import { assessScenario, type ScenarioAssessment, supportsScenario } from "./assess-scenario";
import type { ScenarioRepository } from "./repository";
import type { ScenarioIndex, ScenarioIndexEntry } from "./schema";

export type ScenarioAssessments = Readonly<Record<string, ScenarioAssessment>>;

export function assessmentKey(entry: ScenarioIndexEntry): string {
  return `${entry.id}:${entry.trace_ref}:${entry.trace_sha256}`;
}

export function useScenarioAssessments(
  repository: ScenarioRepository,
  index: ScenarioIndex,
): ScenarioAssessments {
  const [assessments, setAssessments] = useState<ScenarioAssessments>({});
  useEffect(() => {
    const controller = new AbortController();
    let timer: number;
    let remaining = index.runs.filter(supportsScenario);
    const check = async () => {
      const retry: ScenarioIndexEntry[] = [];
      await Promise.all(
        remaining.map(async (entry) => {
          const result = await repository.loadTrace(entry, controller.signal);
          if (controller.signal.aborted) return;
          const assessment: ScenarioAssessment = result.ok
            ? assessScenario(entry, result.value)
            : {
                state: "unavailable",
                label: "Not verified",
                detail: "Recording could not be checked. Retrying…",
              };
          setAssessments((previous) => ({ ...previous, [assessmentKey(entry)]: assessment }));
          if (!result.ok) retry.push(entry);
        }),
      );
      if (controller.signal.aborted) return;
      remaining = retry;
      if (remaining.length) timer = window.setTimeout(check, 3_000);
    };
    setAssessments({});
    void check();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [index, repository]);
  return assessments;
}
