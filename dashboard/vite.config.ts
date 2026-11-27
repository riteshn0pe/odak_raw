// Vite config. Dev server proxies /v1, /admin, and /health to the API so the
// browser sees one origin and CORS behaves the same locally as in production.
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

const API_TARGET = process.env.VITE_API_TARGET ?? "http://localhost:8000";

// Where the built SPA is mounted. "/" is the dev server and any host that gives
// the SPA its own origin; "/app/" is the same-origin container, where the API
// keeps "/" for the landing page and "/login" for the OAuth callback. Vite
// rewrites asset URLs with this and reports it as import.meta.env.BASE_URL, so
// the router basename follows without a second setting.
const BASE = process.env.VITE_BASE ?? "/";

// `/admin/...` is both a staff API prefix and the staff console's own route tree
// (e.g. /admin/login, /admin/plans). Browser navigations must reach the SPA, while
// fetch/XHR must reach the API — so route by Accept: a document request asks for
// text/html, fetch defaults to */*.
function spaOrApi(req: { method?: string; headers: { accept?: string } }): string | undefined {
  const accept = req.headers.accept ?? "";
  if (req.method === "GET" && accept.includes("text/html")) return "/index.html";
  return undefined; // fall through to the proxy
}

export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: BASE,
  // The API origin, baked into the bundle so the SPA can send a signed-out
  // visitor to the landing page's sign-in form (the login surface — the SPA has
  // no /login route of its own). Same VITE_API_TARGET the dev proxy uses, so
  // the two can never disagree; empty string = same-origin deployment.
  define: { __API_ORIGIN__: JSON.stringify(API_TARGET) },
  server: {
    port: 5173,
    proxy: {
      "/v1": { target: API_TARGET, changeOrigin: true },
      "/admin": { target: API_TARGET, changeOrigin: true, bypass: spaOrApi },
      "/health": { target: API_TARGET, changeOrigin: true },
      // API-served assets/pages so they resolve from the dashboard origin too:
      // the API docs screen opens these, and the widget snippet loads /widget.js.
      "/openapi.json": { target: API_TARGET, changeOrigin: true },
      "/docs": { target: API_TARGET, changeOrigin: true },
      "/widget.js": { target: API_TARGET, changeOrigin: true },
    },
  },
  build: { outDir: "dist", sourcemap: true },
});
