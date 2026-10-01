# Vector Logs: web console

React 18 + TypeScript + Vite, the same design and components as the ES Config Console. The API serves the built files at `/ui/`; the Docker build compiles them in its first stage, and a built copy is kept in `../app/static/ui` so local runs work without Node.

```bash
npm install
npm run dev        # http://localhost:5173/ui/, proxies /api to http://localhost:8080 (API_URL=... to change)
npm run build      # type-check and build into ../app/static/ui
```

| File | What |
| --- | --- |
| `src/pages/Logs.tsx` | The search page: query, time range, filters, histogram, top values, results, detail, export, save, copy link |
| `src/components/Histogram.tsx` | Stacked bars by level (SVG), hover tooltip, click to zoom |
| `src/pages/Saved.tsx` | Your saved searches |
| `src/pages/admin/Users.tsx`, `Clusters.tsx` | Admin pages |
| `src/pages/Login.tsx`, `ChangePassword.tsx`, `components/ui.tsx`, `styles.css` | Shared with ES-API |
| `src/api.ts`, `session.tsx`, `format.ts` | API client, session gate, number and time formatting |

The search state lives in the URL (`start`, `end`, `q`, `f` = filters as JSON, `order`, `size`, `off`, `cols`), which is what makes links shareable and what a saved search stores.

Level colours: Error and Warn use the fixed status colours (always next to a text label); Info, Debug and Other use categorical colours checked for colour-vision deficiency in light and dark mode (`--lv-*` in `styles.css`).
