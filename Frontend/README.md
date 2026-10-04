# Cortex — Frontend

The client half of Cortex. **Plain HTML/CSS/JavaScript — no build step, no npm
dependencies, no local engine.** Every answer comes from the backend API; a dead
backend shows an honest Offline state instead of fabricating replies.

Both pages use a light-first Quartz palette, retain the dark theme, and share the
saved `cortex.theme` preference. The landing preview is explicitly illustrative;
its library links, topic suggestions, and walkthrough excerpts come from `/corpus`.
The assistant's routing card follows the selected answer, including historical
citations. Activity insights show backend counts and user-reported feedback, not
fabricated routing accuracy.

Conversation history lives in `localStorage` (`cortex.sessions.v1`): search, rename,
confirmed deletion (browser copy only — backend logs and handoff tickets are not
deleted, and the dialog says so), Today/Yesterday/Earlier groups, and a saved draft
per conversation. The right inspector is sources-first — a quiet empty state before
the first answer, then cited documents and retrieved evidence, with pipeline stages
secondary under "Processing details".

## Keyboard controls

- `Enter` sends from the message box; `Shift+Enter` inserts a new line.
- `Alt+N` starts a conversation, including while the composer is focused.
- `N` starts a conversation only when not typing in an editable field.
- `/` outside editable fields, or `Ctrl/Cmd+K`, focuses the composer.
- New-conversation actions focus the composer and confirm the action. Requests in
  progress block starting another conversation. Modal dialogs retain keyboard focus.
- Shortcuts match by physical key (`e.code`), so they work on any keyboard layout.
  Composition keystrokes and held Enter do not submit messages.

## Pages

| File | Serves | What it is |
|---|---|---|
| `landing.html` + `landing.css` | `/` | Product landing — illustrative preview, clickable corpus topics, corpus-backed walkthrough, document-upload story, real counts, connectivity status |
| `index.html` | `/app` | The assistant — chat, searchable/manageable conversation history, sources-first inspector, corpus browser, metrics drawer |
| `styles.css` | shared | Design tokens, dark/light themes, responsive, `prefers-reduced-motion` |
| `app.js` | `/app` | The client logic — SSE streaming, sessions + drafts, historical routing/evidence inspector, citations, related-doc chips, feedback, handoff tickets, corpus upload UI, `?q=` / `?panel=` deep links |
| `extraui.txt` | — | UI snippet library (folder/pencil animation source — not loaded at runtime) |

## How it connects

The backend serves these files same-origin (`api.py` mounts `frontend/` at `/`),
so browser calls pass the same-origin auth check with zero config:

```
GET  /health        → live/offline pill
POST /query/stream  → SSE: stage / skill / error / result events
GET  /corpus        → corpus browser + landing topics/walkthrough + related-doc chips
GET  /domains       → scope chips + domain metadata
POST /feedback      → resolved/unresolved buttons
POST /corpus/upload → drag-drop document add (md/txt/pdf/docx)
GET  /metrics, /models → metrics drawer
```

`?api=<url>` overrides the base for a separate server (cross-origin posts are
rejected by the backend — same-origin serving is the supported path).

## Running

Nothing to build. From the backend:

```bash
cd ../backend && python api.py --mode demo --port 8000
# landing → http://127.0.0.1:8000/   assistant → http://127.0.0.1:8000/app
```

Static preview without the API (UI only — send is honestly disabled):

```bash
python -m http.server 55258 --bind 127.0.0.1 --directory .
```

## Checks (run from `backend/`)

```bash
node --check ../frontend/app.js
node --test tests/frontend.test.cjs
# browser suite (spawns its own demo backend, needs Edge/Playwright):
npm exec --yes --package=@playwright/test@1.55.1 --call "node tests/frontend.browser.cjs"
```

## Hard rule

There must never be a code path that answers a query without the API — no
bundled corpus, classifier, retrieval, merge, or verify simulation. If the
backend is unreachable, the UI says so.
