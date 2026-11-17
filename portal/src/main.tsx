import { createRoot } from "react-dom/client";
import { initToken } from "./api";
import App from "./App";
import "./styles.css";

initToken().finally(() => createRoot(document.getElementById("root")!).render(<App />));
