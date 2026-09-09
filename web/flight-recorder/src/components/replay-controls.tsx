import { useEffect } from "react";
import type { ReplayActions, ReplayState } from "../replay/use-replay";
import styles from "./inspection-views.module.css";

export interface ReplayControlsProps {
  readonly actions: ReplayActions;
  readonly announce: boolean;
  readonly eventCount: number;
  readonly reducedMotion: boolean;
  readonly state: ReplayState;
}

export function ReplayControls(props: ReplayControlsProps) {
  const { actions, announce, eventCount, reducedMotion, state } = props;
  useReplayKeys(actions, state.isPlaying);
  return (
    <section aria-label="Replay controls" className={styles.replayControls}>
      <button disabled={state.atStart} onClick={actions.previous} type="button">
        Previous event
      </button>
      <button
        disabled={reducedMotion || (!state.isPlaying && state.atEnd)}
        onClick={state.isPlaying ? actions.pause : actions.play}
        type="button"
      >
        {state.isPlaying ? "Pause replay" : "Play replay"}
      </button>
      <button disabled={state.atEnd} onClick={actions.next} type="button">
        Next event
      </button>
      <button disabled={state.atStart} onClick={actions.restart} type="button">
        Restart replay
      </button>
      <output
        aria-live={announce && !state.isPlaying ? "polite" : "off"}
        className={styles.replayPosition}
      >
        Event {state.cursor + 1} of {eventCount}
      </output>
      {reducedMotion ? <p>Motion preference is active; use Previous and Next.</p> : null}
    </section>
  );
}

function useReplayKeys(actions: ReplayActions, isPlaying: boolean): void {
  useEffect(() => {
    const handle = (event: KeyboardEvent) => handleReplayKey(event, actions, isPlaying);
    window.addEventListener("keydown", handle);
    return () => window.removeEventListener("keydown", handle);
  }, [actions, isPlaying]);
}

function handleReplayKey(event: KeyboardEvent, actions: ReplayActions, isPlaying: boolean): void {
  if (isInteractive(event.target)) return;
  const action = replayKeyAction(event.key, actions, isPlaying);
  if (!action) return;
  event.preventDefault();
  action();
}

function replayKeyAction(
  key: string,
  actions: ReplayActions,
  isPlaying: boolean,
): (() => void) | undefined {
  if (key === "ArrowLeft") return actions.previous;
  if (key === "ArrowRight") return actions.next;
  if (key === "Home") return actions.restart;
  if (key === " ") return isPlaying ? actions.pause : actions.play;
  return undefined;
}

function isInteractive(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return Boolean(
    target.closest("a, button, input, select, summary, textarea, [contenteditable], [role]"),
  );
}
