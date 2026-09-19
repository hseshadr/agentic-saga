import { useEffect } from "react";
import { documentBuildVersion, type ScenarioRepository } from "./scenarios/repository";

export function useBuildUpdates(repository: ScenarioRepository, reload: () => void): void {
  useEffect(() => {
    const currentVersion = documentBuildVersion(document);
    const controller = new AbortController();
    let timer: number;
    const check = async () => {
      const result = await repository.loadBuildVersion(controller.signal);
      if (controller.signal.aborted) return;
      if (result.ok && result.value !== "[]" && result.value !== currentVersion) {
        reload();
        return;
      }
      timer = window.setTimeout(check, 3_000);
    };
    timer = window.setTimeout(check, 3_000);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [repository, reload]);
}
