import { useEffect, useState } from "react";

const QUERY = "(prefers-reduced-motion: reduce)";

export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(() => window.matchMedia?.(QUERY).matches ?? false);
  useEffect(() => {
    if (!window.matchMedia) return;
    const query = window.matchMedia(QUERY);
    const update = (event: MediaQueryListEvent) => setReduced(event.matches);
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  return reduced;
}
