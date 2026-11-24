# AI Structural Engineering Office — Starter

Local-first starter: React/Vite frontend + FastAPI backend + Pydantic JSON contract.

## Safety
This is software infrastructure, not validated structural analysis or code-compliant design. The `.std` output is geometry-only. Simulation returns a NOT_ANALYZED status and no fabricated forces or drift. Engineer review and independent verification are mandatory.

## Run on Windows
Requirements: Python 3.10+, Node.js 20+, npm.

Backend (PowerShell, from project root):
```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt
uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000
```

Frontend (second PowerShell):
```powershell
cd frontend
npm install
npm run dev -- --host 0.0.0.0
```
Open the Vite URL printed in the terminal. API docs: http://localhost:8000/docs.

## Tablet access
Connect tablet and PC to the same trusted Wi-Fi. Run `ipconfig` on Windows, then open `http://PC-IP:5173` on the tablet. If prompted, allow the app only on a private network. Do not expose the development server to the public internet.

## Included
- Project schema validation and reference checks
- JSON project storage
- CAD plan X/Y + elevation -> STAAD X/Y/Z coordinate mapping
- Geometry-only `.std` preview
- Explicit placeholder simulation endpoint
- Responsive dashboard

## API
`GET /api/health`, `GET /api/projects`, `POST /api/projects`, `GET /api/projects/{id}`, `POST /api/projects/{id}/validate`, `POST /api/projects/{id}/staad-preview`, `POST /api/projects/{id}/simulate`.

Next: DXF import, reviewed load assignment, validated STAAD/OpenSTAAD adapter, result extraction, independent design checks, report and drawing generation.
