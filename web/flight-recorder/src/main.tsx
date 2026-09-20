import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./app";
import { ScenarioRepository } from "./scenarios/repository";
import { useBuildUpdates } from "./use-build-updates";
import "./styles/global.css";

const container = document.querySelector("#root");
if (!container) throw new Error("Flight Recorder root element is missing.");

const repository = new ScenarioRepository(new URL("./traces/", window.location.href));
const reload = () => window.location.reload();

function RecorderPage() {
  useBuildUpdates(repository, reload);
  return <App repository={repository} />;
}

createRoot(container).render(
  <StrictMode>
    <RecorderPage />
  </StrictMode>,
);
