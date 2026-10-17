# TicketZero

Recursive autonomous support engineer for Crusoe Managed Inference tickets. See [instruction.md](instruction.md) for the full spec.

## Run

```bash
cp .env.example .env            # then put your keys in .env (single quotes: Crusoe keys can contain $)

# backend
python3 -m venv .venv && .venv/bin/pip install fastapi "uvicorn[standard]" pydantic openai requests pytest
.venv/bin/uvicorn ticketzero.api:app --host 127.0.0.1 --port 8000

# frontend (Node 20.19+ / 22.12+), in a second terminal
cd frontend && npm ci && npm run dev            # open the address Vite prints
```

Both the backend and the frontend dev server read settings from `.env` in the project root; variables exported in
your shell override it. `.env` is git-ignored. The dev server proxies `/api/*` to the backend at `BACKEND_URL`
(default `http://127.0.0.1:8000`).

## Working folders

Every run works on a folder: pick one under "Your folder" (Choose… opens the macOS folder picker, or type a path),
or click a demo, which uses `ticketzero/scenario_data/<name>/repo`.

- The folder needs pytest tests (`tests/`, `test_*.py` or `*_test.py`) that fail now and should pass once fixed;
  they become the frozen acceptance tests.
- The run copies the folder into a sandbox (`.git`, `node_modules`, virtualenvs, caches and secret-looking files
  such as `.env` and `*.pem` are left out) and never edits the folder itself. A verified change is written back only
  if you clicked "Allow file edits" before starting, and only if the touched files haven't changed since the run
  started (`POST /runs/{id}/apply`).
- If the folder has `.venv/bin/python` (or `venv/`, `env/`) with pytest installed, tests run with it, so the
  project's own dependencies are available.
- Before any fix, the frozen tests run once on the unmodified code. If they already pass, the reported failure does
  not reproduce (for example a past platform outage): nothing is patched, and the run investigates and escalates.

Demos: three hand-made scenarios plus the TicketZero benchmark tickets (the "Benchmark tickets" list on the start
page), each a folder under `ticketzero/scenario_data/`.

Manual testing: `manual_testing/` holds the original ticket catalog. Pick one folder under `manual_testing/tickets/`
with "Your folder" and describe the problem yourself; `build_catalog.py`, `score.py` and the answer key live there too
(see `manual_testing/TICKETS_README.md`).

Tests, no keys or network needed:
- Backend: `.venv/bin/python -m pytest` (scripted fake Nemotron drives full runs)
- Frontend: `cd frontend && npm test` (flowchart layout, escaping, milestones) and `npm run build`

## Layout

```
ticketzero/                 backend package
  api.py                    FastAPI routes and startup
  runner.py                 run lifecycle: background thread, root resolve, final report
  resolver.py               the one recursive function (split / leaf / replan / failure children)
  agents.py                 role calls: triage, planner, investigator tool loop, evidence gate, fixer
  verify.py                 frozen test runner and Independent Verifier (tests + critic veto)
  sandbox.py                git base repo, one worktree per node, frozen-file protection
  patches.py                diff parsing, lenient apply, patch safety validation
  tools.py                  investigator tools (files, logs, tests, /v1/models, You.com)
  llm.py                    Crusoe client: budget charge, rate limiter, retries, JSON repair
  models.py                 Nemotron model routing resolved from live /v1/models; cost estimates
  research.py               You.com search and contents, cached in SQLite
  prompts.py, schemas.py    role system prompts; structured output schemas
  budget.py, tasks.py       run/node budgets and run state; task and result types
  store.py                  SQLite store and shared memory (context_for)
  reports.py                usage, execution tree, report and escalation bundle
  scenarios.py              loads scenario_data/
  scenario_data/<name>/     scenario.json, logs.txt, repo/ (tests/ and pytest.ini are the frozen tests)
frontend/                   HTML/CSS/JS app, built into one page (build.mjs); talks to the backend over /api
  ticketzero-ui.js          live investigation view, flowchart, evidence dialogs, outcome
  ticketzero-model.js       flowchart layout, activity milestones, journey stage (tested in qa/)
  ticketzero-folder-ui.js   project folder via the backend: picker, file browser, edit permission
  ticketzero-api.js         backend client
tests/                      offline test suite
```
