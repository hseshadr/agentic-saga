import { useEffect, useMemo, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { projectReplay } from "../replay/project-replay";
import { useReducedMotion } from "../replay/use-reduced-motion";
import { useReplay } from "../replay/use-replay";
import type { ScenarioIndex, ScenarioIndexEntry } from "../scenarios/schema";
import { assessmentKey, type ScenarioAssessments } from "../scenarios/use-scenario-assessments";
import { causalEventIds } from "../trace/flight-projection";
import type { RunTrace, TraceEvent } from "../trace/schema";
import { EventDetails } from "./event-details";
import { FlightPath } from "./flight-path";
import { LedgerView } from "./ledger-view";
import { OutcomeHeader } from "./outcome-header";
import { ProofView } from "./proof-view";
import styles from "./recorder-workbench.module.css";
import { ReplayControls } from "./replay-controls";
import { SagaFlow } from "./saga-flow";
import { ScenarioBadge, ScenarioResults } from "./scenario-results";
import { StoryView } from "./story-view";
import { type RecorderView, ViewTabs } from "./view-tabs";

export interface RecorderWorkbenchProps {
  readonly assessments?: ScenarioAssessments;
  readonly entry: ScenarioIndexEntry;
  readonly index: ScenarioIndex;
  readonly onSelectRun: (runId: string) => void;
  readonly trace: RunTrace;
}

interface FocusReturn {
  readonly element: HTMLElement;
  readonly view: RecorderView;
}

export function RecorderWorkbench(props: RecorderWorkbenchProps) {
  const { assessments = {}, entry, index, onSelectRun, trace } = props;
  const reducedMotion = useReducedMotion();
  const pauseAfter = useMemo(() => replayWaypoints(trace), [trace]);
  const replay = useReplay({
    eventCount: trace.events.length,
    initialCursor: "end",
    pauseAfter,
    reducedMotion,
  });
  const projection = useMemo(
    () => projectReplay(trace, replay.state.cursor),
    [replay.state.cursor, trace],
  );
  const recordedOutcome = useMemo(() => projectReplay(trace, trace.events.length - 1), [trace]);
  const [selectedEventId, setSelectedEventId] = useState(projection.currentEvent.event_id);
  const [activeView, setActiveView] = useState<RecorderView>("story");
  const [returnTarget, setReturnTarget] = useState<FocusReturn | null>(null);
  const inspectorRef = useRef<HTMLElement>(null);
  const changeView = (view: RecorderView) => {
    replay.actions.pause();
    setActiveView(view);
  };
  useEffect(
    () => setSelectedEventId(projection.currentEvent.event_id),
    [projection.currentEvent.event_id],
  );
  const selectEvent = (eventId: string) => {
    const active = document.activeElement;
    if (active instanceof HTMLElement) setReturnTarget({ element: active, view: activeView });
    selectRecordedEvent(eventId, trace, replay.actions.seek, setSelectedEventId, () =>
      inspectorRef.current?.focus(),
    );
  };
  const selected = projection.events.find((event) => event.event_id === selectedEventId);
  const causalIds = causalEventIds(projection.flight, selectedEventId);

  return (
    <main className={styles.workbench}>
      <OutcomeHeader entry={entry} projection={recordedOutcome} />
      <p className={styles.authorityChain}>
        {provenanceDescription(entry.mode)} Replay only reads recorded evidence. The local server
        stays open to serve this page.
      </p>
      <ScenarioResults assessments={assessments} index={index} />
      {entry.presentation === "ecommerce" ? (
        <SagaFlow isPlaying={replay.state.isPlaying} projection={projection} />
      ) : null}
      <div className={styles.workspace}>
        <RunTrajectory
          assessments={assessments}
          entry={entry}
          index={index}
          onSelect={onSelectRun}
        />
        <section aria-label="Flight recorder workspace" className={styles.centerPanel}>
          <ReplayControls
            actions={replay.actions}
            announce={announcesTransition(projection.currentEvent)}
            eventCount={trace.events.length}
            reducedMotion={reducedMotion}
            state={replay.state}
          />
          <FlightPath
            causalIds={causalIds}
            onSelect={selectEvent}
            projection={projection.flight}
            selectedEventId={selectedEventId}
          />
          <ViewTabs active={activeView} onChange={changeView} />
          <EvidenceViews active={activeView} onSelectEvent={selectEvent} projection={projection} />
        </section>
        <EventDetails
          className={styles.inspector}
          event={selected}
          ref={inspectorRef}
          {...(returnTarget ? { onReturnFocus: () => restoreFocus(returnTarget) } : {})}
        />
      </div>
    </main>
  );

  function restoreFocus(target: FocusReturn): void {
    flushSync(() => setActiveView(target.view));
    target.element.focus();
    setReturnTarget(null);
  }
}

const announcedEvents = new Set([
  "compensation_started",
  "human_required",
  "proposal_rejected",
  "terminal_assigned",
]);

function announcesTransition(event: TraceEvent): boolean {
  return event.policy_decision !== null || announcedEvents.has(event.event_type);
}

function replayWaypoints(trace: RunTrace): ReadonlySet<number> {
  const waypoints = new Set([
    "compensation_completed",
    "compensation_started",
    "human_required",
    "invariant_evaluated",
    "proposal_rejected",
    "reconciliation_recorded",
  ]);
  return new Set(
    trace.events.flatMap((event, index) => (waypoints.has(event.event_type) ? [index] : [])),
  );
}

function selectRecordedEvent(
  eventId: string,
  trace: RunTrace,
  seek: (cursor: number) => void,
  select: (eventId: string) => void,
  focus: () => void,
): void {
  const cursor = trace.events.findIndex((event) => event.event_id === eventId);
  if (cursor < 0) return;
  seek(cursor);
  select(eventId);
  focus();
}

function EvidenceViews(props: {
  readonly active: RecorderView;
  readonly onSelectEvent: (eventId: string) => void;
  readonly projection: ReturnType<typeof projectReplay>;
}) {
  const { active, onSelectEvent, projection } = props;
  return (
    <>
      <div
        aria-labelledby="view-story-tab"
        hidden={active !== "story"}
        id="view-story-panel"
        role="tabpanel"
      >
        <StoryView events={projection.flight.orderedEvents} onSelectEvent={onSelectEvent} />
      </div>
      <div
        aria-labelledby="view-ledger-tab"
        hidden={active !== "ledger"}
        id="view-ledger-panel"
        role="tabpanel"
      >
        <LedgerView events={projection.flight.orderedEvents} onSelectEvent={onSelectEvent} />
      </div>
      <div
        aria-labelledby="view-proof-tab"
        hidden={active !== "proof"}
        id="view-proof-panel"
        role="tabpanel"
      >
        <ProofView onSelectEvent={onSelectEvent} projection={projection} />
      </div>
    </>
  );
}

interface TrajectoryProps {
  readonly assessments?: ScenarioAssessments;
  readonly entry: ScenarioIndexEntry;
  readonly index: ScenarioIndex;
  readonly onSelect: (runId: string) => void;
}

export function RunTrajectory({ assessments = {}, entry, index, onSelect }: TrajectoryProps) {
  return (
    <nav aria-label="Run trajectory" className={styles.trajectory}>
      <h2>Choose a scenario</h2>
      <p>{index.runs.length} recorded scenarios. Select one to inspect its outcome and replay.</p>
      <ul>
        {index.runs.map((run) => (
          <li key={run.id}>
            <button
              aria-current={run.id === entry.id ? "page" : undefined}
              onClick={() => onSelect(run.id)}
              type="button"
            >
              <ScenarioBadge entry={run} assessments={assessments} />
              <strong>{run.name}</strong>
              <span>{assessments[assessmentKey(run)]?.detail ?? run.summary}</span>
              <small>{recordingLabel(run.mode)}</small>
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}

function recordingLabel(mode: ScenarioIndexEntry["mode"]): string {
  if (mode === "scripted") return "Scripted recording";
  if (mode === "live") return "Live agent recording";
  return "Agent provenance not recorded";
}

function provenanceDescription(mode: ScenarioIndexEntry["mode"]): string {
  if (mode === "scripted") return "Scripted recording · No live model calls · JEV is not used.";
  if (mode === "live")
    return "Recording from a live agent run · Model provider is not recorded in this catalog.";
  return "Agent provenance not recorded. Model use and provider are unknown.";
}
