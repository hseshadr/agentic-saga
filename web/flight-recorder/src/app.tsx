import { type ReactNode, useEffect, useRef, useState } from "react";
import styles from "./app.module.css";
import { RecorderWorkbench, RunTrajectory } from "./components/recorder-workbench";
import type { LoadResult, ScenarioRepository } from "./scenarios/repository";
import type { ScenarioIndex, ScenarioIndexEntry } from "./scenarios/schema";
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
  const state = useIndex(repository);
  if (state.kind === "failed")
    return <AppFrame content={<LoadMessage kind="error" message={state.message} />} />;
  if (state.kind === "pending")
    return (
      <AppFrame content={<LoadMessage kind="loading" message="Loading recorded evidence…" />} />
    );
  return <AppFrame content={<ReadyRecorder index={state.value} repository={repository} />} />;
}

function AppFrame({ content }: { readonly content: ReactNode }) {
  return (
    <>
      <a className={styles.skipLink} href="#flight-recorder-content">
        Skip to flight recorder
      </a>
      <div className={styles.content} id="flight-recorder-content" tabIndex={-1}>
        {content}
      </div>
    </>
  );
}

function ReadyRecorder({ index, repository }: ReadyProps) {
  const [selectedId, setSelectedId] = useState(index.runs[0]?.id ?? "");
  const entry = index.runs.find((run) => run.id === selectedId) ?? index.runs[0];
  const state = useTrace(repository, entry);
  if (!entry) return <LoadMessage kind="error" message="Trace index contains no runs." />;
  if (state.kind === "failed")
    return (
      <RunLoadMessage
        entry={entry}
        index={index}
        message={state.message}
        onSelect={setSelectedId}
      />
    );
  if (state.kind === "pending") {
    return (
      <RunLoadMessage
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
      entry={entry}
      index={index}
      key={entry.id}
      onSelectRun={setSelectedId}
      trace={state.value}
    />
  );
}

interface RunLoadMessageProps {
  readonly entry: ScenarioIndexEntry;
  readonly index: ScenarioIndex;
  readonly kind?: "error" | "loading";
  readonly message: string;
  readonly onSelect: (runId: string) => void;
}

function RunLoadMessage(props: RunLoadMessageProps) {
  const { entry, index, kind = "error", message, onSelect } = props;
  const messageRef = useRef<HTMLParagraphElement>(null);
  useEffect(() => {
    if (kind === "error") messageRef.current?.focus();
  }, [kind]);
  return (
    <main className={styles.runLoadState}>
      <RunTrajectory entry={entry} index={index} onSelect={onSelect} />
      <section className={styles.runLoadMessage}>
        <h1>Saga Flight Recorder</h1>
        <p ref={messageRef} role={kind === "error" ? "alert" : "status"} tabIndex={-1}>
          {message}
        </p>
      </section>
    </main>
  );
}

function useIndex(repository: ScenarioRepository): LoadState<ScenarioIndex> {
  const [state, setState] = useState<LoadState<ScenarioIndex>>(pending);
  useEffect(() => loadEffect((signal) => repository.loadIndex(signal), setState), [repository]);
  return state;
}

function useTrace(
  repository: ScenarioRepository,
  entry: ScenarioIndexEntry | undefined,
): LoadState<RunTrace> {
  const [state, setState] = useState<LoadState<RunTrace>>(pending);
  useEffect(() => {
    if (!entry) return;
    setState(pending);
    return loadEffect((signal) => repository.loadTrace(entry, signal), setState);
  }, [entry, repository]);
  return state;
}

function loadEffect<Value>(
  load: (signal: AbortSignal) => Promise<LoadResult<Value>>,
  update: (state: LoadState<Value>) => void,
): () => void {
  const controller = new AbortController();
  void load(controller.signal).then((result) => {
    if (!controller.signal.aborted) update(toLoadState(result));
  });
  return () => controller.abort();
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
