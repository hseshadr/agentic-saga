import { supportsScenario } from "../scenarios/assess-scenario";
import type { ScenarioIndex, ScenarioIndexEntry } from "../scenarios/schema";
import { assessmentKey, type ScenarioAssessments } from "../scenarios/use-scenario-assessments";
import styles from "./recorder-workbench.module.css";

export function ScenarioBadge({
  entry,
  assessments,
}: {
  readonly entry: ScenarioIndexEntry;
  readonly assessments: ScenarioAssessments;
}) {
  const result = assessments[assessmentKey(entry)];
  const label = result?.label ?? (supportsScenario(entry) ? "Checking…" : "Not assessed");
  return (
    <span className={styles.scenarioBadge} data-result={result?.state ?? "pending"}>
      <span aria-hidden="true">
        {result?.state === "passed" ? "✓" : result?.state === "failed" ? "!" : "○"}
      </span>{" "}
      {label}
    </span>
  );
}

export function ScenarioResults({
  index,
  assessments,
}: {
  readonly index: ScenarioIndex;
  readonly assessments: ScenarioAssessments;
}) {
  const runs = index.runs.filter(supportsScenario);
  if (!runs.length) return null;
  const passed = runs.filter(
    (entry) => assessments[assessmentKey(entry)]?.state === "passed",
  ).length;
  return (
    <section aria-label="Use case results" className={styles.scenarioResults}>
      <div>
        <h2>
          {passed} / {runs.length} use cases passed
        </h2>
        <p>
          Passed means the expected safety behavior was verified, including recovery when an order
          fails.
        </p>
      </div>
      <ul>
        {runs.map((entry) => (
          <li key={entry.id}>
            <div>
              <ScenarioBadge entry={entry} assessments={assessments} />
              <strong>{entry.name}</strong>
              <small>
                {assessments[assessmentKey(entry)]?.detail ?? "Checking recorded evidence…"}
              </small>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
