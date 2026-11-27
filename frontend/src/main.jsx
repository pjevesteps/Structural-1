import React from "react";
import { createRoot } from "react-dom/client";
import { installApiFetchDefaults } from "./api.js";
import App from "./App.jsx";
import "./style.css";

installApiFetchDefaults();
createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
