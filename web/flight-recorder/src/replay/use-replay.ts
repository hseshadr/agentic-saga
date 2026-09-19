import { useEffect, useState } from "react";

export interface ReplayState {
  readonly atEnd: boolean;
  readonly atStart: boolean;
  readonly cursor: number;
  readonly isPlaying: boolean;
}

export interface ReplayActions {
  readonly next: () => void;
  readonly pause: () => void;
  readonly play: () => void;
  readonly previous: () => void;
  readonly restart: () => void;
  readonly seek: (cursor: number) => void;
  readonly watch: () => void;
}

export interface ReplayOptions {
  readonly eventCount: number;
  readonly initialCursor?: "end" | number;
  readonly intervalMs?: number;
  readonly pauseAfter: ReadonlySet<number>;
  readonly reducedMotion?: boolean;
}

export function useReplay(options: ReplayOptions): {
  readonly actions: ReplayActions;
  readonly state: ReplayState;
} {
  const { eventCount, intervalMs = 700, pauseAfter, reducedMotion = false } = options;
  const end = Math.max(0, eventCount - 1);
  const [cursor, setCursor] = useState(() => initialCursor(options.initialCursor, end));
  const [isPlaying, setIsPlaying] = useState(false);
  useEffect(() => setCursor((value) => Math.min(value, end)), [end]);
  useEffect(() => {
    if (!isPlaying || reducedMotion || cursor >= end) return;
    const timer = window.setTimeout(() => {
      const next = Math.min(cursor + 1, end);
      setCursor(next);
      if (next >= end || pauseAfter.has(next)) setIsPlaying(false);
    }, intervalMs);
    return () => window.clearTimeout(timer);
  }, [cursor, end, intervalMs, isPlaying, pauseAfter, reducedMotion]);
  useEffect(() => {
    if (reducedMotion || cursor >= end) setIsPlaying(false);
  }, [cursor, end, reducedMotion]);
  const seek = (value: number) => {
    setIsPlaying(false);
    setCursor(Math.max(0, Math.min(Math.trunc(value), end)));
  };
  const state = { atEnd: cursor >= end, atStart: cursor === 0, cursor, isPlaying };
  const actions = replayActions(cursor, end, reducedMotion, seek, setCursor, setIsPlaying);
  return { actions, state };
}

function initialCursor(value: "end" | number | undefined, end: number): number {
  if (value === "end") return end;
  return Math.max(0, Math.min(Math.trunc(value ?? 0), end));
}

function replayActions(
  cursor: number,
  end: number,
  reducedMotion: boolean,
  seek: (cursor: number) => void,
  setCursor: (cursor: number) => void,
  setPlaying: (playing: boolean) => void,
): ReplayActions {
  return {
    next: () => seek(cursor + 1),
    pause: () => setPlaying(false),
    play: () => {
      if (!reducedMotion && cursor < end) setPlaying(true);
    },
    previous: () => seek(cursor - 1),
    restart: () => seek(0),
    seek,
    watch: () => {
      setCursor(0);
      setPlaying(!reducedMotion && end > 0);
    },
  };
}
