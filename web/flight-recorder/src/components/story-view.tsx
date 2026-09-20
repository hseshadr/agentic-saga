import type { ProjectedEvent } from "../trace/flight-projection";
import type { TraceEvent } from "../trace/schema";
import styles from "./inspection-views.module.css";

export interface StoryViewProps {
  readonly events: readonly ProjectedEvent[];
  readonly onSelectEvent: (eventId: string) => void;
}

const maximumStoryEvents = 200;
const summaries: Readonly<Record<string, string>> = {
  agent_turn_reserved: "Agent turn reserved",
  compensation_intent_recorded: "Undo requested; outcome not yet recorded",
  compensation_started: "Recovery started",
  dispatch_started: "Provider request sent; outcome not yet recorded",
  effect_intent_recorded: "Change requested; outcome not yet recorded",
  read_observed: "Provider response recorded",
  read_started: "Provider read requested",
  saga_created: "Workflow record created",
  saga_started: "Workflow started",
};
const completedTools: Readonly<Record<string, string>> = {
  reserve_inventory: "Inventory reserved",
  charge_payment: "Payment charged",
  schedule_fulfillment: "Delivery arranged",
  cancel_fulfillment: "Delivery cancelled",
  refund_payment: "Refund completed",
  release_inventory: "Inventory released",
};
interface EventResult {
  readonly label: "Recorded" | "Completed" | "Failed" | "Uncertain" | "Verified" | "Needs review";
  readonly detail: string;
}

export function StoryView({ events, onSelectEvent }: StoryViewProps) {
  const storyEvents = events.slice(-maximumStoryEvents);
  return (
    <section aria-labelledby="story-heading" className={styles.view}>
      <h2 id="story-heading">Recorded story</h2>
      <p>
        Each row shows what happened at that step. Later recovery does not change earlier results.
      </p>
      {storyEvents.length < events.length ? (
        <p>
          Showing the latest {storyEvents.length} of {events.length} visible events.
        </p>
      ) : null}
      <div className={styles.tableFrame}>
        <table aria-label="Causal story" className={styles.story}>
          <thead>
            <tr>
              {["Step", "Action", "Responsible", "Result", "Evidence"].map((label) => (
                <th key={label} scope="col">
                  {label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {storyEvents.map((item) => {
              const result = resultFor(item.event);
              return (
                <tr key={item.event.event_id}>
                  <td data-label="Step">{String(item.event.saga_seq).padStart(2, "0")}</td>
                  <td data-label="Action">
                    <strong>{actionFor(item)}</strong>
                  </td>
                  <td data-label="Responsible">{item.laneLabel}</td>
                  <td data-label="Result">
                    <div className={styles.storyResult}>
                      <span className={styles.storyStatus} data-result={result.label}>
                        {result.label}
                      </span>
                      <span>{result.detail}</span>
                    </div>
                  </td>
                  <td data-label="Evidence">
                    <button
                      aria-label={`Inspect story event ${item.event.saga_seq}`}
                      onClick={() => onSelectEvent(item.event.event_id)}
                      type="button"
                    >
                      Inspect
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function actionFor(item: ProjectedEvent): string {
  if (
    item.event.tool_name &&
    ["effect_outcome_recorded", "compensation_outcome_recorded", "invariant_evaluated"].includes(
      item.event.event_type,
    )
  )
    return item.event.tool_name
      .replaceAll("_", " ")
      .replace(/^./, (letter) => letter.toUpperCase());
  return item.label;
}

function resultFor(event: TraceEvent): EventResult {
  const kind = event.redacted_output?.kind;
  if (event.event_type === "human_required")
    return { label: "Needs review", detail: "Human review requested before further changes" };
  if (event.event_type === "proposal_rejected")
    return { label: "Failed", detail: "Proposed action rejected by safety rules" };
  if (event.event_type === "invariant_evaluated") return invariantResult(event);
  if (kind === "effect_confirmed" || kind === "reconcile_effect_confirmed") {
    const detail = completedTools[event.tool_name ?? ""] ?? "Provider confirmed the change";
    return {
      label: "Completed",
      detail:
        kind === "reconcile_effect_confirmed" ? `${detail}; confirmed by reconciliation` : detail,
    };
  }
  if (kind === "no_effect_confirmed" || kind === "reconcile_no_effect_confirmed")
    return { label: "Failed", detail: "Provider confirmed no change was made" };
  if (kind === "partial_effect_confirmed")
    return { label: "Uncertain", detail: "Only part of the requested change was confirmed" };
  if (kind === "outcome_unknown" || kind === "reconcile_pending" || kind === "reconcile_conflict")
    return { label: "Uncertain", detail: "Provider outcome not confirmed at this step" };
  if (kind === "unsupported" || kind === "reconcile_unsupported")
    return { label: "Uncertain", detail: "Provider could not reconcile the outcome" };
  if (event.event_type === "terminal_assigned") return terminalResult(event);
  if (event.event_type === "agent_decision_recorded")
    return { label: "Recorded", detail: "Agent decision recorded; no action result in this event" };
  return {
    label: "Recorded",
    detail: summaries[event.event_type] ?? "Evidence recorded; no confirmed result in this event",
  };
}

function invariantResult(event: TraceEvent): EventResult {
  const verified =
    event.redacted_output?.verified ?? event.receipt?.verified ?? event.rationale.all_passed;
  const subject = event.tool_name === "verify_order" ? "Order verification" : "Safety check";
  if (verified === false) return { label: "Failed", detail: `${subject} failed` };
  if (verified === true) return { label: "Verified", detail: `${subject} passed` };
  return { label: "Recorded", detail: "Safety evidence recorded; result not specified" };
}

function terminalResult(event: TraceEvent): EventResult {
  if (event.after_status === "compensated_verified")
    return { label: "Verified", detail: "Recovery completed; earlier changes safely undone" };
  if (event.after_status === "succeeded_verified")
    return { label: "Verified", detail: "Workflow completed; final state verified" };
  if (event.after_status === "aborted_clean")
    return { label: "Recorded", detail: "Workflow stopped without leaving changes" };
  return { label: "Recorded", detail: "Workflow outcome recorded" };
}
