# TicketZero

The complete source for the TicketZero frontend demo, including the latest local-project-folder feature. The interface uses HTML, CSS, and JavaScript. Vite serves local development; the build produces a standalone HTML file with embedded fonts and icons.

## Quick start

Install Node.js 22.12 or newer (Node 20.19 is also supported), extract this archive, then run these commands inside the `ticketzero` folder:

```bash
npm ci
npm run dev
```

Open the local address printed by Vite. Keep the terminal running. Press Ctrl+C to stop.

You can also open the included `index.html` directly without installing anything. Local-folder features depend on browser support; serving through the development command is the preferred workflow.

## Editing and building

Edit the source files below. After saving source changes, run `npm run build` in another terminal and refresh the browser. The development command builds once when it starts; it does not continuously rebuild the embedded source files.

```bash
npm run build
npm test
```

The build writes `index.html`, a self-contained app (styles, scripts, fonts and icons embedded).

## Source files

- `ticketzero-shell.html`: page layout and accessible dialogs.
- `ticketzero.css`: visual design and responsive styles, including the divide-and-conquer flowchart.
- `ticketzero-api.js`: client for the TicketZero backend (`/api`, proxied by Vite to `BACKEND_URL`).
- `ticketzero-model.js`: pure helpers: flowchart layout and rendering, activity milestones, journey stage.
- `ticketzero-ui.js`: intake, live investigation view, flowchart, evidence dialogs, outcome and report export.
- `ticketzero-folder-ui.js`: project folder via the backend (native picker, file browsing, edit permission).
- `build.mjs`: embeds styles, scripts, fonts, and icons into one HTML file.
- `qa/model.test.cjs`: tests for the flowchart layout, HTML escaping, milestones, and stages.
- `package.json`, `package-lock.json`, `vite.config.mjs`: dependencies, development commands, `/api` proxy.
- `licenses/`: notices for the embedded fonts and icons.

## How it connects

Start the backend first (`uvicorn ticketzero.api:app --port 8000` from the project root), then `npm run dev`.

- "Start investigating" runs a live investigation. An example runs on its built-in project folder; your own issue
  needs a project folder with pytest tests ("Choose folder" opens the native macOS picker through the backend).
- The main panel shows the execution tree as a flowchart: each box is one agent; parents wait for their children,
  merge fixes and test again. Select a box to see its task, evidence, change and verification.
- The folder is never edited during a run. With "Allow file edits", a fix that passes every acceptance test is
  written back at the end, unless the touched files changed in the meantime.
- Opening the built `index.html` directly from disk also works; it then calls the backend at http://127.0.0.1:8000.
