import { type ReactNode, useEffect, useRef, useState } from "react";
import styles from "./app.module.css";
import { RecorderWorkbench, RunTrajectory } from "./components/recorder-workbench";
import type { LoadResult, ScenarioRepository } from "./scenarios/repository";
import type { ScenarioIndex, ScenarioIndexEntry } from "./scenarios/schema";
import {
  type ScenarioAssessments,
  useScenarioAssessments,
} from "./scenarios/use-scenario-assessments";
import type { RunTrace } from "./trace/schema";

export interface AppProps {
  readonly repository: ScenarioRepository;
}

interface ReadyProps extends AppProps {
  readonly index: ScenarioIndex;
}

type LoadState<Value> =
  | { readonly kind: "failed"; readonly message: string }
  | { readonly kind: "pending" }
  | { readonly kind: "ready"; readonly value: Value };
const pending: LoadState<never> = { kind: "pending" };

export function App({ repository }: AppProps) {
  const { state, freshness, lastChecked } = useIndex(repository);
  const status = (
    <div aria-label="Recording updates" className={styles.freshness} role="status">
      <strong>{freshness}</strong>
      <span>Auto-refresh checks recordings every 3 seconds.</span>
      {lastChecked ? <span>Last checked {lastChecked.toLocaleTimeString()}</span> : null}
    </div>
  );
  if (state.kind === "failed")
    return (
      <AppFrame content={<LoadMessage kind="error" message={state.message} />} status={status} />
    );
  if (state.kind === "pending")
    return (
      <AppFrame
        content={<LoadMessage kind="loading" message="Loading recorded evidence…" />}
        status={status}
      />
    );
  return (
    <AppFrame
      content={<ReadyRecorder index={state.value} repository={repository} />}
      status={status}
    />
  );
}

function AppFrame({
  content,
  status,
}: {
  readonly content: ReactNode;
  readonly status: ReactNode;
}) {
  return (
    <>
      <a className={styles.skipLink} href="#flight-recorder-content">
        Skip to flight recorder
      </a>
      <div className={styles.content} id="flight-recorder-content" tabIndex={-1}>
        {status}
        {content}
      </div>
    </>
  );
}

function ReadyRecorder({ index, repository }: ReadyProps) {
  const [selectedId, setSelectedId] = useState(index.default_run_id ?? index.runs[0]?.id ?? "");
  const entry = index.runs.find((run) => run.id === selectedId) ?? index.runs[0];
  const state = useTrace(repository, entry);
  const assessments = useScenarioAssessments(repository, index);
  if (!entry) return <LoadMessage kind="error" message="Trace index contains no runs." />;
  if (state.kind === "failed")
    return (
      <RunLoadMessage
        assessments={assessments}
        entry={entry}
        index={index}
        message={state.message}
        onSelect={setSelectedId}
      />
    );
  if (state.kind === "pending") {
    return (
      <RunLoadMessage
        assessments={assessments}
        entry={entry}
        index={index}
        kind="loading"
        message="Loading selected RunTrace…"
        onSelect={setSelectedId}
      />
    );
  }
  return (
    <RecorderWorkbench
      assessments={assessments}
      entry={entry}
      index={index}
      key={`${entry.id}:${entry.trace_sha256}`}
      onSelectRun={setSelectedId}
      trace={state.value}
    />
  );
}

interface RunLoadMessageProps {
  readonly assessments: ScenarioAssessments;
  readonly entry: ScenarioIndexEntry;
  readonly index: ScenarioIndex;
  readonly kind?: "error" | "loading";
  readonly message: string;
  readonly onSelect: (runId: string) => void;
}

function RunLoadMessage(props: RunLoadMessageProps) {
  const { assessments, entry, index, kind = "error", message, onSelect } = props;
  const messageRef = useRef<HTMLParagraphElement>(null);
  useEffect(() => {
    if (kind === "error") messageRef.current?.focus();
  }, [kind]);
  return (
    <main className={styles.runLoadState}>
      <RunTrajectory assessments={assessments} entry={entry} index={index} onSelect={onSelect} />
      <section className={styles.runLoadMessage}>
        <h1>Saga Flight Recorder</h1>
        <p ref={messageRef} role={kind === "error" ? "alert" : "status"} tabIndex={-1}>
          {message}
        </p>
      </section>
    </main>
  );
}

function useIndex(repository: ScenarioRepository) {
  const [state, setState] = useState<LoadState<ScenarioIndex>>(pending);
  const [freshness, setFreshness] = useState("Checking for updates");
  const [lastChecked, setLastChecked] = useState<Date | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    let timer: number;
    const check = async () => {
      setFreshness("Checking for updates");
      const result = await repository.loadIndex(controller.signal);
      if (controller.signal.aborted) return;
      if (result.ok) {
        setState((previous) =>
          previous.kind === "ready" &&
          JSON.stringify(previous.value) === JSON.stringify(result.value)
            ? previous
            : toLoadState(result),
        );
        setFreshness("Up to date");
        setLastChecked(new Date());
      } else {
        setFreshness("Reconnecting — showing last available recording");
        setState((previous) => (previous.kind === "ready" ? previous : toLoadState(result)));
      }
      timer = window.setTimeout(check, 3_000);
    };
    void check();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [repository]);
  return { state, freshness, lastChecked };
}

function useTrace(
  repository: ScenarioRepository,
  entry: ScenarioIndexEntry | undefined,
): LoadState<RunTrace> {
  const key = entry ? `${entry.id}:${entry.trace_ref}:${entry.trace_sha256}` : "";
  const [snapshot, setSnapshot] = useState<{ key: string; state: LoadState<RunTrace> }>({
    key: "",
    state: pending,
  });
  const entryRef = useRef(entry);
  entryRef.current = entry;
  useEffect(() => {
    const selected = entryRef.current;
    if (!selected) return;
    const controller = new AbortController();
    let timer: number;
    const load = async () => {
      const result = await repository.loadTrace(selected, controller.signal);
      if (controller.signal.aborted) return;
      setSnapshot({ key, state: toLoadState(result) });
      if (!result.ok) timer = window.setTimeout(load, 3_000);
    };
    void load();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [key, repository]);
  return snapshot.key === key ? snapshot.state : pending;
}

function toLoadState<Value>(result: LoadResult<Value>): LoadState<Value> {
  return result.ok
    ? { kind: "ready", value: result.value }
    : { kind: "failed", message: result.message };
}

interface LoadMessageProps {
  readonly kind: "error" | "loading";
  readonly message: string;
}

function LoadMessage({ kind, message }: LoadMessageProps) {
  return (
    <main className={styles.loadState}>
      <h1>Saga Flight Recorder</h1>
      <p role={kind === "error" ? "alert" : "status"}>{message}</p>
    </main>
  );
}
