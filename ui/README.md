# ChatDemo UI

Chat UI built with **React + Vite + Tailwind CSS v4**, styled with **shadcn/ui**
components (Radix primitives + `class-variance-authority` + `lucide-react`
icons) — all public, open-source packages, no private registry required. Name
login, multiple sessions, multi-turn (backend owns history per `session_id`),
source links, related-question chips, live step trace, and the confirm flow
(reply `yes`/`no`).

## Develop

```bash
cd ui
npm install
npm run dev                 # http://localhost:5173 (proxies /chat -> :8000)
```
Start the backend separately (`cd ../chatdemo && uv run chatdemo`). The dev server
proxies `/chat` and `/health` to `http://localhost:8000`, so leave the login
"Backend" field blank (same origin).

## Build + Docker image (deploy)

The React app is built **outside** Docker; the image just serves `dist/` via nginx
(and reverse-proxies the API), so the container stays small and self-contained.

```bash
cd ui
npm install
npm run build               # -> ui/dist/
docker build -t chatdemo-ui ./ui          # copies dist/ into nginx
# or from the repo root:  docker compose up --build   (build dist/ first!)
```

Image env (override per environment):
- `BACKEND_URL` — where `/chat`, `/chat/stream`, `/health` are proxied
  (default `http://backend:8000`).
- `DNS_RESOLVER` — DNS for request-time upstream resolution (default `127.0.0.11`,
  Docker's embedded DNS; set to your cluster DNS on k8s/ECS). Request-time
  resolution means the UI starts even if the backend isn't up yet.

## Layout

- `src/` — React app: `App.jsx`, `components/` (`Login`, `Sidebar`, `Chat`,
  `Message`, `PianoNav`, `Markdown`), `components/ui/` (shadcn primitives:
  `button`, `card`, `input`, `textarea`, `avatar`, `separator`, `tooltip`,
  `sheet`, `collapsible`, `badge`), `lib/store.js` (localStorage + backend
  calls), `lib/utils.js` (`cn()` class-merge helper), `global.css` (Tailwind +
  theme tokens).
- `index.html` — Vite entry.
- `vite.config.js` — React + Tailwind plugins, `/chat` dev proxy.
- `Dockerfile` + `default.conf.template` — nginx image serving `dist/` + API proxy.

## Theming

Every color in the UI reads from CSS variables defined in `src/global.css`
(`:root` for light mode, `.dark` for dark mode) and mapped in the `@theme
inline` block. To change the accent color, edit the two `--primary` /
`--primary-foreground` lines — nothing else needs to change. The rest of the
palette (background, card, muted, border, etc.) follows the same pattern, so a
full re-skin is just editing values in one place.
