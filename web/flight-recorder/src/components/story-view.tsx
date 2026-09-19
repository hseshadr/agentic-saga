import type { ProjectedEvent } from "../trace/flight-projection";
import styles from "./inspection-views.module.css";

export interface StoryViewProps {
  readonly events: readonly ProjectedEvent[];
  readonly onSelectEvent: (eventId: string) => void;
}

const maximumStoryEvents = 200;

const summaries: Readonly<Record<string, string>> = {
  agent_turn_reserved: "A bounded agent turn was recorded.",
  compensation_intent_recorded: "A reverse effect intent was recorded before dispatch.",
  compensation_outcome_recorded: "The recorded undo outcome was captured.",
  compensation_started: "The workflow started safe compensation.",
  dispatch_started: "Provider dispatch entry was recorded.",
  effect_intent_recorded: "An effect intent was durably recorded before dispatch.",
  effect_outcome_recorded: "The provider outcome was recorded.",
  human_required: "The workflow paused safely for human review.",
  invariant_evaluated: "Deterministic invariant evidence was recorded.",
  proposal_rejected: "Policy rejected the recorded proposal.",
  read_observed: "A provider read result was recorded.",
  read_started: "A read intent was recorded.",
  reconciliation_recorded: "Reconciliation evidence was recorded.",
  saga_created: "The durable Saga record was created.",
  saga_started: "The Temporal workflow started the recorded Saga.",
  terminal_assigned: "The workflow assigned a verified terminal state.",
};

export function StoryView({ events, onSelectEvent }: StoryViewProps) {
  const storyEvents = events.slice(-maximumStoryEvents);
  const bounded = storyEvents.length < events.length;
  return (
    <section aria-labelledby="story-heading" className={styles.view}>
      <h2 id="story-heading">Recorded story</h2>
      <p>Plain-language landmarks derived only from durable event types and state.</p>
      {bounded ? (
        <p>
          Showing the latest {storyEvents.length} of {events.length} visible events.
        </p>
      ) : null}
      <ol aria-label="Causal story" className={styles.story}>
        {storyEvents.map((item) => (
          <li key={item.event.event_id}>
            <button
              aria-label={`Inspect story event ${item.event.saga_seq}`}
              className={styles.storySignal}
              onClick={() => onSelectEvent(item.event.event_id)}
              type="button"
            >
              <strong>
                {String(item.event.saga_seq).padStart(2, "0")} {item.label}
              </strong>
              <span className={styles.storyLane}>{item.laneLabel}</span>
              <span className={styles.storySummary}>{summaryFor(item)}</span>
              <span className={styles.storyStatus}>
                {item.event.after_status.replaceAll("_", " ")}
              </span>
            </button>
          </li>
        ))}
      </ol>
    </section>
  );
}

function summaryFor(item: ProjectedEvent): string {
  if (item.event.event_type === "agent_decision_recorded") return `${item.label}.`;
  return summaries[item.event.event_type] ?? "Recorded ledger evidence.";
}
