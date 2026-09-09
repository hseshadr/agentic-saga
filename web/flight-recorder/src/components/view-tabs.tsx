import type { KeyboardEvent } from "react";
import styles from "./inspection-views.module.css";

export type RecorderView = "ledger" | "proof" | "story";

const views: readonly RecorderView[] = ["story", "ledger", "proof"];

export interface ViewTabsProps {
  readonly active: RecorderView;
  readonly onChange: (view: RecorderView) => void;
}

export function ViewTabs({ active, onChange }: ViewTabsProps) {
  return (
    <div aria-label="Evidence views" className={styles.tabs} role="tablist">
      {views.map((view) => (
        <button
          aria-controls={`view-${view}-panel`}
          aria-selected={active === view}
          id={`view-${view}-tab`}
          key={view}
          className={styles.tab}
          onClick={() => onChange(view)}
          onKeyDown={(event) => moveTab(event, view, onChange)}
          role="tab"
          tabIndex={active === view ? 0 : -1}
          type="button"
        >
          {title(view)}
        </button>
      ))}
    </div>
  );
}

function moveTab(
  event: KeyboardEvent<HTMLButtonElement>,
  current: RecorderView,
  change: (view: RecorderView) => void,
): void {
  const target = keyTarget(event.key, current);
  if (!target) return;
  event.preventDefault();
  change(target);
  document.querySelector<HTMLButtonElement>(`#view-${target}-tab`)?.focus();
}

function keyTarget(key: string, current: RecorderView): RecorderView | undefined {
  if (key === "Home") return views[0];
  if (key === "End") return views.at(-1);
  const delta = key === "ArrowLeft" ? -1 : key === "ArrowRight" ? 1 : 0;
  if (!delta) return undefined;
  const index = views.indexOf(current);
  return views[(index + delta + views.length) % views.length];
}

function title(view: RecorderView): string {
  return view.replace(/^./, (letter) => letter.toUpperCase());
}
