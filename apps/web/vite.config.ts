import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server runs in the `web` container; port 5173 is published by docker-compose.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, host: "0.0.0.0" },
});
