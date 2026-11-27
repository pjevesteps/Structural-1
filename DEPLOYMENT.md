# Cloud & Tablet Deployment Guide (Railway)

This repository packages **Structural AI Office** as a single-container full-stack service (FastAPI backend + compiled React/Vite frontend) designed for 24/7 cloud and tablet access on [Railway](https://railway.app/).

## 1. Deploy on Railway

1. Create a **New Project** in Railway and choose **Deploy from GitHub repo** pointing to this repository.
2. Railway automatically detects [`railway.json`](railway.json) and builds the multi-stage [`Dockerfile`](Dockerfile):
   - **Stage 1 (`node:20-slim`)**: builds `frontend/dist` with `npm ci && npm run build`.
   - **Stage 2 (`python:3.11-slim`)**: installs `backend/requirements.txt`, copies `backend/` and `frontend/dist`, and serves both API and SPA from a single `uvicorn` process.
3. Attach a **Persistent Volume**:
   - In your Railway service settings → **Volumes** → **Add Volume**.
   - Set **Mount Path** to `/app/data`.
4. Set **Environment Variables** in Railway → **Variables**:
   - `DATA_DIR=/app/data`
   - *(Optional)* `CORS_ORIGINS=https://<your-app>.up.railway.app` (or `*` if accessed from external clients without credentials).
   - **Phase 1 Beta Authentication & Per-User Isolation (Optional)**:
     - `AUTH_ENABLED=true` (omit or set `false` for open local development)
     - `AUTH_SECRET=<random-64-char-hex-secret>` (keeps signed `sao_session` cookies valid across container restarts)
     - `BETA_USERS=paul:StrongPass1,engr_juan:StrongPass2` (comma-separated `username:password` pairs; each user's projects, imports, and exports are isolated under `/app/data/users/<username>/`)
     - `BOT_API_KEY=<random-api-key>` and optional `BOT_API_USER=paul` (allows `my-bots-room` or automation clients to authenticate via the `X-API-Key` header without a browser cookie)
5. Generate a public domain under **Settings → Networking → Generate Domain**.
6. Verify deployment health at:
   ```text
   https://<your-app>.up.railway.app/api/health
   ```
   Expected response:
   ```json
   {"status": "ok", "version": "0.1.0", "data_dir": "/app/data"}
   ```

## 2. Connect `my-bots-room` (Building AI Agent)

To let the **Building AI Agent** (`mode: "structural_office"`) in `my-bots-room` drive your 24/7 cloud instance instead of local port `8000`, set:

```bash
STRUCTURAL_OFFICE_URL=https://<your-app>.up.railway.app
```

in your `my-bots-room` environment before starting `server.py`.

## 3. Local Development (Unchanged)

- **Backend**: `uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000`
- **Frontend**: `npm --prefix frontend run dev` (Vite proxies `/api` to `http://127.0.0.1:8000`)
- Local data continues to live in `./data/` when `DATA_DIR` is unset.
