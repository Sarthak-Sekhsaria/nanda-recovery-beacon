# Deploying NANDA Recovery Beacon to a public HTTPS URL

This gets the service onto a persistent public URL on **Render**, backed by a free **Neon**
PostgreSQL database. Total time: about 10 minutes. No credit card required.

The same Neon database is used for the test run and for production — tests live in an isolated
`beacon_test` schema, so they never touch production tables.

---

## Step 1 — Create the Neon database (≈2 min)

1. Go to <https://neon.tech> and sign in (GitHub sign-in is fine).
2. **Create a project.** Name it `recovery-beacon`. Any region.
3. On the project dashboard, find **Connection string** and copy it. It looks like:

   ```
   postgresql://neondb_owner:XXXXXXXX@ep-cool-name-12345.us-east-2.aws.neon.tech/neondb?sslmode=require
   ```

4. Keep this string handy. You will use it twice: once for the test run, once as `DATABASE_URL`.

> The `postgres://` / `postgresql://` form is fine — the service normalises it automatically.

---

## Step 2 — Push to GitHub (≈2 min)

The repository is already committed locally. Create an empty GitHub repo and push:

1. Go to <https://github.com/new>. Name it `nanda-recovery-beacon`. **Do not** add a README,
   `.gitignore`, or license (the repo already has them). Create it.
2. Copy the commands GitHub shows under **"…or push an existing repository"**, or run:

   ```bash
   cd "C:/Users/Sarthak/OneDrive/Documents/NandaHackRecoveryBeacon"
   git remote add origin https://github.com/<your-username>/nanda-recovery-beacon.git
   git push -u origin main
   ```

3. If prompted to authenticate, use your GitHub username and a
   [personal access token](https://github.com/settings/tokens) as the password (classic token with
   `repo` scope is enough).

---

## Step 3 — Deploy on Render (≈4 min)

1. Go to <https://render.com> and sign in with GitHub.
2. **New → Blueprint.**
3. Select your `nanda-recovery-beacon` repository. Render reads [`render.yaml`](render.yaml) and
   shows two services: `nanda-recovery-beacon-api` and `nanda-recovery-beacon-dashboard`.
4. Render will prompt for the environment variables marked `sync: false`. Set:

   | Service | Variable | Value |
   | --- | --- | --- |
   | api | `DATABASE_URL` | your Neon connection string from Step 1 |
   | api | `PUBLIC_BASE_URL` | leave blank for now (set in Step 4) |
   | dashboard | `BEACON_API_URL` | leave blank for now (set in Step 4) |
   | dashboard | `NEXT_PUBLIC_BEACON_PUBLIC_URL` | leave blank for now (set in Step 4) |

5. **Apply.** Render builds both services. The API's start command runs `alembic upgrade head`
   automatically, so the schema (including the partial unique index and the append-only triggers)
   is created on first boot.

Wait until the **api** service is **Live**. Note its URL, e.g.
`https://nanda-recovery-beacon-api.onrender.com`.

---

## Step 4 — Wire the URLs together (≈2 min)

Now that the API has a public URL, tell it (and the dashboard) what it is.

1. **api** service → **Environment** → set:
   - `PUBLIC_BASE_URL` = `https://nanda-recovery-beacon-api.onrender.com`
2. **dashboard** service → **Environment** → set both:
   - `BEACON_API_URL` = `https://nanda-recovery-beacon-api.onrender.com`
   - `NEXT_PUBLIC_BEACON_PUBLIC_URL` = `https://nanda-recovery-beacon-api.onrender.com`
3. Both services redeploy automatically. Wait for **Live**.

`PUBLIC_BASE_URL` is what gets substituted into `/skill.md`, so after this step the skill document
advertises the correct URL to agents.

---

## Step 5 — Seed sample data (optional, ≈1 min)

So the dashboard and recovery queue have something to show during judging:

- Render **api** service → **Shell** tab, then run:

  ```bash
  python -m app.cli seed
  ```

  It prints API keys for the sample agents. (The service defaults to `DEMO_MODE=true` in
  `render.yaml`, so you can also just curl it without a key.)

---

## Step 6 — Verify the live deployment

From your machine:

```bash
cd "C:/Users/Sarthak/OneDrive/Documents/NandaHackRecoveryBeacon"
bash scripts/verify_deployment.sh https://nanda-recovery-beacon-api.onrender.com
```

This drives the whole lifecycle against the live service — create → checkpoint → fail → discover →
claim → resume → checkpoint → complete — and checks the audit trail and security headers. It should
end with `failed: 0`.

Then open the endpoints a judge will check:

| URL | Expect |
| --- | --- |
| `https://…onrender.com/health` | `{"status":"ok",…}` |
| `https://…onrender.com/ready` | `{"status":"ready","database":"ok",…}` |
| `https://…onrender.com/skill.md` | the SKILL.md, with the real base URL substituted |
| `https://…onrender.com/docs` | Swagger UI |
| `https://…onrender.com/api/v1/recoverable-workflows` | JSON (needs a key unless demo mode) |
| the dashboard URL | the recovery control center, populated with live data |

---

## Turning off demo mode (for real use)

`render.yaml` ships with `DEMO_MODE=true` so judges can curl the API without provisioning a key.
For a deployment holding real work:

1. api service → set `DEMO_MODE=false`.
2. In the api **Shell**, mint keys:

   ```bash
   python -m app.cli create-key --agent-id planner-1
   python -m app.cli create-admin-key --agent-id admin      # if you need admin endpoints
   ```

3. If the dashboard should keep working, give it a key: set `BEACON_API_KEY` on the dashboard
   service to a key minted for `beacon-dashboard`.
4. Restrict `CORS_ALLOW_ORIGINS` and `TRUSTED_HOSTS` to your domains.

---

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| api build fails on `psycopg` | Ensure `PYTHON_VERSION` is 3.12.x (set in `render.yaml`). |
| `/ready` returns 503 `migrations_applied: false` | The start command runs `alembic upgrade head`; check the deploy logs for a migration error, usually a bad `DATABASE_URL`. |
| First request takes ~30s | Free instances sleep after 15 min idle. Normal. It wakes on the first request. |
| `/skill.md` still shows `{{PUBLIC_BASE_URL}}` | `PUBLIC_BASE_URL` isn't set on the api service (Step 4). |
| dashboard shows "Could not load data" | `BEACON_API_URL` isn't set, or points at the wrong URL, or the API is asleep (retry in 30s). |
| verify script: `command not found: jq` | Install jq, or run the checks from the table above manually. |
