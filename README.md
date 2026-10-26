# Otiscord API

## Local setup

1. Link the existing Neon project from the workspace root if it is not already linked, then apply the root `neon.ts` configuration:

   ```powershell
   neon link --project-id <project-id> -y
   neon deploy
   ```

   `neon deploy` provisions the private attachment bucket and writes the branch's connection strings to the root `.env.local`; keep it private. Use the intended development branch while testing and production only when ready.
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

   The API uses the pooled `DATABASE_URL`; Alembic prefers `DATABASE_URL_UNPOOLED`. Run `alembic upgrade head` against the intended branch before deploying a backend release that includes a new migration. Revision `0003_profile_presence` adds profile and online-status fields. Check `http://127.0.0.1:8000/api/health` and `http://127.0.0.1:8000/docs`.

Set `ENVIRONMENT=development` in `backend\.env` to enable the interactive API docs at `/docs` (and `/redoc`). With any other value, including `production`, the docs and OpenAPI schema endpoints return 404. Do not set it to `development` in the Vercel production environment.

Set `VITE_API_URL=http://127.0.0.1:8000` in `frontend/.env.local`, then start the Vite app with `npm run dev`.

## File uploads

The root `neon.ts` declares a private `otiscord-attachments` bucket. Apply it to the intended Neon branch with `neon deploy`; the bucket is branch-scoped. After applying, pull that branch's `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_ENDPOINT_URL_S3`, and `AWS_REGION` into the backend's private `.env`. Keep the bucket private: the API issues short-lived signed URLs after checking conversation membership.

Configure the bucket's CORS rules for the frontend's local and production origins. Allow `GET`, `PUT`, and `HEAD`, allow the `Content-Type` and `Range` request headers, and expose `ETag`, `Content-Length`, `Content-Range`, and `Accept-Ranges` so direct uploads and video seeking work in the browser. Do not use `*` for allowed origins in production; add your exact Vercel production domain and any preview origin pattern you use.

The composer keeps selected files as a local draft, shows image/video previews and file details, and lets the user remove files before sending. On send, the browser uploads bytes directly to object storage, verifies the stored size and content type, then attaches the file to the message. If the message request fails after upload, the uploaded draft remains available for retry. Any file type is accepted, with a maximum size of 50 MiB per file; up to 10 files can be attached to one message. Supported images, video, and audio are served inline through expiring signed URLs; other file types download as attachments.

If the backend health response has `storage_configured: false`, file and avatar uploads are unavailable until the backend environment has all four `AWS_*` credentials above. Configure those values in the backend's Vercel project (and redeploy); never add them to the frontend.

For the Vercel backend, add these four `AWS_*` values and `ATTACHMENTS_BUCKET` to the backend project's environment variables for each environment that should accept uploads. Neon CLI env pulls update local files only; they do not configure Vercel. Keep all storage credentials and `AUTH_SECRET` on the backend, never in the frontend project. Use different database branches and storage credentials for development and production.

Never commit `.env` or put a Neon password in frontend code. Keep `AUTH_SECRET` private and use a different strong secret for production. Email verification, password recovery, and voice/video calling need their own trusted services and are not enabled by this API.
