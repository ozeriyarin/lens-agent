# Deploying lens-agent (Fly.io)

Live since 2026-10-25 at <https://lens-agent.fly.dev> (app `lens-agent`, region `fra`,
volume `lens_data`).

The server runs as one Docker container on [Fly.io](https://fly.io): a public HTTPS URL
(the phone camera needs HTTPS), a 1 GB persistent volume at `/data` for the SQLite ledger,
the entity memory and the `kb/` notes, and a machine that stops itself when nobody is
scanning. Config is `fly.toml` at the repo root; the image is the repo's own `Dockerfile`.

## Why Fly and not Render or Vercel

| | fits? | monthly hosting |
|---|---|---|
| Vercel | no: serverless functions, no long-running process, no persistent disk | - |
| Render | yes, but the free tier has no disk and sleeps; Starter (512 MB) 7 USD + disk 0.25 USD/GB | ~7.25 USD |
| **Fly.io** | yes: `shared-cpu-1x` 512 MB about 3.3 USD if it never stopped, volume 0.15 USD/GB | **~0.5 to 3.5 USD** |

With `auto_stop_machines = "stop"` the machine runs only while someone is using the page
(plus a few idle minutes), and a stopped machine is billed only for its root filesystem
(0.15 USD/GB). For a personal project used a few times a week the realistic bill is well
under 1 USD; the ceiling if it ran all month is about 3.5 USD. Fly bills a card at the end
of the month. This hosting cost is **on top of** the constitution's 5 USD/month cap, which
covers API (model) spend only. Prices checked 2026-10-25 at fly.io/docs/about/pricing.

Why the non-root Dockerfile works unchanged: Fly sets the owner of the mounted volume to the
image's `USER` (`lens`, uid 1000), so `/data` is writable without an entrypoint `chown`.

## One-time setup (owner, about ten minutes)

Fly has no "create from Dockerfile" button in the dashboard; the CLI does that part. Nothing
below prints a secret, and none of it is in the repo.

1. Install the CLI and sign up (a card is required even for a sub-dollar bill):

   ```powershell
   winget install --id Fly.Flyctl
   fly auth signup                   # or `fly auth login` if you already have an account
   ```

2. Create the app from the committed config. `lens-agent` is probably taken as a name;
   pick another when asked (it becomes `https://<name>.fly.dev`). Say **no** to a Postgres
   or Redis database and **no** to deploying now.

   ```powershell
   cd C:\ai-projects\lens-agent
   fly launch --copy-config --no-deploy
   ```

   This rewrites the `app =` line in `fly.toml`; commit that change.

3. Create the volume in the same region as the app (one volume, one machine: the ledger
   must not be split):

   ```powershell
   fly volumes create lens_data --region fra --size 1 --yes
   ```

4. Set the secrets. Generate the scan token locally; paste the API keys from the provider
   consoles. These go to Fly's encrypted store, not to the repo.

   ```powershell
   $token = -join ((1..40) | ForEach-Object { '{0:x}' -f (Get-Random -Max 16) })
   fly secrets set GOOGLE_API_KEY=<key> ANTHROPIC_API_KEY=<key> SCAN_TOKEN=$token
   $token                             # copy it for the phone URL in step 6
   ```

   Optional: `fly secrets set LOGFIRE_TOKEN=<token>` for tracing.

5. Deploy. `--ha=false` matters: by default Fly creates **two** machines for redundancy,
   which would need two volumes and would split the ledger and the memory in half.

   ```powershell
   fly deploy --ha=false
   fly status
   ```

   The build runs on Fly's builders (the laptop does not need Docker). First deploy takes
   two to four minutes; later ones reuse the cached layers.

6. Check it, then open it on the phone **once** with the token in the URL fragment:

   ```
   https://<name>.fly.dev/health          -> {"status":"ok","model":"google:gemini-2.5-flash",...}
   https://<name>.fly.dev/#token=<token>  -> on the phone; allow the camera, tap Scan
   ```

   The fragment never reaches the server or its logs; the page stores the token in local
   storage and strips it from the address bar. Without the token every scan is a 401 and
   costs nothing.

## Day to day

```powershell
fly logs                       # the JSON scan events, one line per scan
fly ssh console                # a shell in the machine; the data is under /data
fly ssh sftp get /data/lens.db # pull the ledger for a look
fly machine stop               # pause by hand (auto-stop does this anyway)
fly scale count 0              # take it offline but keep the volume; `fly scale count 1` to come back
fly apps destroy <name>        # delete everything, including the volume and its data
```

Adding notes for the lookup tool: `fly ssh sftp shell`, then `put mynote.md /data/kb/mynote.md`;
they are indexed at the next start (`fly machine restart`).

## Updating

Push to `main` as usual (CI still runs ruff, the tests and the image build), then:

```powershell
fly deploy --ha=false
```

The volume is not touched by a deploy; the ledger and memory survive.

## Rolling back

Code: every deploy is a release with an image you can redeploy.

```powershell
fly releases --image          # list releases with their image references
fly deploy --image registry.fly.io/<name>:deployment-<id> --ha=false
```

Data: Fly snapshots volumes daily and keeps them five days.

```powershell
fly volumes list
fly volumes snapshots list <vol_id>
fly volumes create lens_data --snapshot-id <snap_id> --region fra --size 1   # restore into a new volume
```

Then point the machine at the new volume (`fly machine update <id> --mount ...`) or destroy
the old volume and redeploy. For a project whose state is a spend ledger and a few remembered
objects, "redeploy on an empty volume" is also an acceptable rollback.

## The tunnel still works

The `cloudflared` / `ngrok` instructions in the README are still fine for a laptop-only
session and still need `SCAN_TOKEN` in `.env`. The live server has its own token, set in step 4.
