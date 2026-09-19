import type { ReplayProjection } from "../replay/project-replay";
import { type FlowStep, projectEcommerceFlow } from "../story/project-ecommerce-flow";
import styles from "./saga-flow.module.css";

export function SagaFlow(props: {
  readonly isPlaying: boolean;
  readonly projection: ReplayProjection;
}) {
  const { isPlaying, projection } = props;
  const flow = projectEcommerceFlow(projection);
  return (
    <section aria-labelledby="ecommerce-saga-heading" className={styles.flow}>
      <header className={styles.heading}>
        <div>
          <p className={styles.eyebrow}>Ecommerce example</p>
          <h2 id="ecommerce-saga-heading">One order. Several systems. One safe outcome.</h2>
        </div>
        <div
          aria-live={isPlaying ? "off" : "polite"}
          className={styles.now}
          data-phase={flow.phase}
          role="status"
        >
          <strong>{flow.headline}</strong>
          <span>{flow.detail}</span>
        </div>
      </header>
      <FlowLane
        label="Move the order forward"
        steps={flow.forward}
        summary="The agent chooses the next useful action from current evidence."
      />
      <div className={styles.turn}>
        <span aria-hidden="true">↳</span>
        <strong>If a later step fails, undo completed changes in reverse order.</strong>
      </div>
      <FlowLane
        label="Make the business safe again"
        reverse
        steps={flow.rollback}
        summary="Safety code—not the model—controls which undo action is allowed next."
      />
    </section>
  );
}

function FlowLane(props: {
  readonly label: string;
  readonly reverse?: boolean;
  readonly steps: readonly FlowStep[];
  readonly summary: string;
}) {
  const { label, reverse = false, steps, summary } = props;
  return (
    <section aria-label={label} className={styles.lane} data-reverse={reverse}>
      <div className={styles.laneHeading}>
        <h3>{label}</h3>
        <p>{summary}</p>
      </div>
      <ol>
        {steps.map((item, index) => (
          <FlowNode item={item} key={item.id} number={index + 1} />
        ))}
      </ol>
    </section>
  );
}

function FlowNode({ item, number }: { readonly item: FlowStep; readonly number: number }) {
  return (
    <li aria-label={`${item.label}: ${stateLabel(item.state)}`} data-state={item.state}>
      <span aria-hidden="true" className={styles.number}>
        {stateMark(item.state, number)}
      </span>
      <span className={styles.nodeCopy}>
        <strong>{item.label}</strong>
        <small>{item.detail}</small>
      </span>
      <span className={styles.state}>{stateLabel(item.state)}</span>
    </li>
  );
}

function stateMark(state: FlowStep["state"], number: number): string {
  if (state === "complete") return "✓";
  if (state === "reversed") return "↶";
  if (state === "failed" || state === "attention") return "!";
  if (state === "checking") return "?";
  return String(number);
}

function stateLabel(state: FlowStep["state"]): string {
  const labels: Readonly<Record<FlowStep["state"], string>> = {
    active: "In progress",
    attention: "Needs a human",
    checking: "Checking provider",
    complete: "Done",
    failed: "Could not complete",
    reversed: "Safely undone",
    waiting: "Waiting",
  };
  return labels[state];
}
