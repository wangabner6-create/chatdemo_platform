import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./global.css";

// ThemeProvider and TooltipProvider are nested inside App itself, which is what allows theme toggling.
ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
