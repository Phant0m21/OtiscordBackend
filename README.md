# Otiscord API

## Local setup

1. Link the Neon project from the workspace root and pull its environment:

   ```powershell
   neon init -y --org-id <org-id> --project-id <project-id> --branch production --services postgres
   neon login
   neon deploy
   ```

   Neon writes the connection strings to the root `.env.local`; keep it private.
2. Copy the **pooled** `DATABASE_URL` and **direct** `DATABASE_URL_UNPOOLED` from the root `.env.local` into `backend\.env`, prefixing each with `postgresql+psycopg://` (keep `?sslmode=require`). Add a private `AUTH_SECRET` of at least 32 characters. `backend\.env.example` documents the remaining settings.
3. In PowerShell, from `backend`:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   uvicorn app.main:app --reload --port 8000
   ```

Apply the versioned schema with the **direct** Neon connection before starting the API:

   ```powershell
   alembic upgrade head
   uvicorn app.main:app --reload --port 8000
   ```

The API uses the pooled `DATABASE_URL`; Alembic prefers `DATABASE_URL_UNPOOLED`. Check `http://127.0.0.1:8000/api/health` and `http://127.0.0.1:8000/docs`.

Set `VITE_API_URL=http://127.0.0.1:8000` in `frontend/.env.local`, then start the Vite app with `npm run dev`.

Never commit `.env` or put a Neon password in frontend code. Keep `AUTH_SECRET` private and use a different strong secret for production. Email verification, password recovery, uploads, and voice/video calling need their own trusted services and are not enabled by this initial API.
