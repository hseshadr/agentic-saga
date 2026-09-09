import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./app";
import { ScenarioRepository } from "./scenarios/repository";
import "./styles/global.css";

const container = document.querySelector("#root");
if (!container) throw new Error("Flight Recorder root element is missing.");

const repository = new ScenarioRepository(new URL("./traces/", window.location.href));
createRoot(container).render(
  <StrictMode>
    <App repository={repository} />
  </StrictMode>,
);
