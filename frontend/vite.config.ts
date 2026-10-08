import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The production build is served by the Python package (`prmap`), so it is
// emitted straight into backend/src/prmap/static.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../backend/src/prmap/static",
    emptyOutDir: true,
    chunkSizeWarningLimit: 1500,
  },
  server: {
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:7420" },
  },
});
