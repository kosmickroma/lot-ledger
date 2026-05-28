# LotLedger — Operations & Deploy Runbook

Practical reference for deploying and running ops **remotely** (e.g. while KK is away),
covering everything an agent or KK needs to ship and verify changes. Command-oriented,
secret-free (references `.env` and env-var *names* only — never paste credential values).

> **Golden safety rule:** deploying to **main = Mike's live production**. Never deploy to
> main without KK's explicit go for that specific change. See "Vacation / freeze posture".

---

## 1. Environments → which branch deploys where

| Env | Cloud Run service | GCP project | Source branch | How it deploys |
|---|---|---|---|---|
| **Preview** | `lot-ledger-preview` | `lot-ledger` | any feature branch | **manual:** `./scripts/promote-preview.sh` |
| **Dev** | `lot-ledger-dev` | `lot-ledger` | `develop` | **auto** on push to `develop` (Cloud Build trigger → `cloudbuild.yaml`). Manual fallback below. |
| **Prod** | `real-estate-map-tool` | `real-estate-map-tool` | `main` | **manual:** `./scripts/promote-prod.sh` (Mike's live app) |

- **All three share the same database** — Mike's prod Cloud SQL
  (`real-estate-map-tool:us-central1:lot-ledger-db`). Preview/dev compute live in the
  `lot-ledger` project but their SQL connection points at Mike's prod DB. So a DB write
  from *any* env hits the one real database — be careful with writes.
- **Merge ladder:** feature branch → `develop` → `main`.
- **Version pill** (bottom of the app sidebar) shows the deployed build: prod = `0.N`,
  preview = `0.N-<branch>-pre`, dev = `dev`/`0.N`. Use it to confirm what's actually live.

---

## 2. gcloud auth (do this first if a deploy fails)

Cloud Build/Run commands need a valid gcloud login. Tokens expire; the symptom is:
`Reauthentication failed. cannot prompt during non-interactive execution.`

Fix (run in an **interactive terminal** — the code-paste step needs it):
```bash
gcloud auth login --no-launch-browser
# open the printed URL in a browser, sign in, paste the verification code back
```
Confirm the active account/project:
```bash
gcloud auth list
gcloud config get-value project
```

---

## 3. Deploy to PREVIEW (safe — isolated, Mike never sees it)

From a checkout of the branch you want to preview:
```bash
cd <worktree-on-the-branch>
./scripts/promote-preview.sh        # ~2-5 min Cloud Build; deploys current checkout
```
- Works from any branch; ships the **current working directory** as the build context.
- Get the URL / confirm it's live:
```bash
gcloud run services describe lot-ledger-preview \
  --region=us-central1 --project=lot-ledger --format='value(status.url)'
```
- Current preview URL: `https://lot-ledger-preview-qa7hokv3ma-uc.a.run.app`
- Version pill will read `0.N-<branch-safe>-pre`.

**Use preview to "eyeball before main."** This is the standard verification gate.

---

## 4. Deploy to DEV (develop)

Dev auto-deploys when `develop` is pushed (Cloud Build trigger using `cloudbuild.yaml` →
`lot-ledger-dev`). After a `git push origin develop`, give it a few minutes.

Manual fallback (if the trigger is off or you want to force it), from a `develop` checkout:
```bash
gcloud builds submit --config=cloudbuild.yaml --project=lot-ledger \
  --substitutions=_APP_VERSION=dev
```

---

## 5. Promote a feature branch → develop

No script — it's a `--no-ff` merge commit (house style: `Merge feat/…: <description>`).
Safest from a throwaway worktree so existing worktrees aren't disturbed:
```bash
git fetch origin
git worktree add /tmp/ll-dev develop
cd /tmp/ll-dev
git merge --ff-only origin/develop                  # sync local develop to origin (no-op if equal)
git merge --no-ff <feature-branch> \
  -m "Merge <feature-branch>: <short description>"
git push origin develop
cd -                                                 # leave the temp worktree
git worktree remove /tmp/ll-dev
```
Pushing `develop` triggers the dev auto-deploy (section 4).

---

## 6. Promote develop → MAIN (PROD) — **GATED**

> This ships to **Mike's live app**. Requires KK's explicit approval for the change.
> During a freeze (section 8) do **not** do this.

```bash
# 1. Merge develop into main (from a main checkout / temp worktree), house-style message:
git worktree add /tmp/ll-main main
cd /tmp/ll-main
git merge --ff-only origin/main
git merge --no-ff develop \
  -m "Promote develop → main (YYYY-MM-DD #N): <summary>. Smoked on preview + approved by KK."
git push origin main
# 2. Deploy the prod build (must be ON main + clean tree; the script enforces this):
./scripts/promote-prod.sh
cd - && git worktree remove /tmp/ll-main
```
- `promote-prod.sh` refuses to run unless you're on `main` with a clean tree (override `FORCE=1`).
- It runs `gcloud builds submit --config=cloudbuild-prod.yaml --project=real-estate-map-tool`.
- Prod has **no auto-trigger** — the merge to main does nothing until you run the script.
- Verify after: load the prod URL, confirm the version pill bumped to the new `0.N`.

---

## 7. Verifying a deploy

```bash
# Which revision/image is live + the version pill value:
gcloud run services describe <service> --region=us-central1 --project=<project> \
  --format='value(status.url, status.latestReadyRevisionName)'
```
Then open the URL and confirm the **version pill** matches what you deployed. If a client
reports stale UI, confirm the pill first — don't assume it's a browser cache issue.

---

## 8. Vacation / freeze posture (current)

When KK is away and wants Mike on a stable build:
- **Default: do NOT promote to main.** Feature branches, preview deploys, and `develop`
  merges/deploys are all fine (they don't touch Mike's prod).
- Only deploy to main if KK explicitly approves that specific change (e.g. an urgent hotfix).
- (See the project memory `merge-hold-vacation-2026-05-28` for the active window.)

---

## 9. Common DB / data ops

All DB access uses creds from `.env` (`DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`,
`DB_NAME`). The main worktree (`/home/kk/projects/clients/lot-ledger`) has the `.env` and
the gitignored `ingest/` + `data/` source files.

**Connect with psql (read/verify):**
```bash
cd /home/kk/projects/clients/lot-ledger
set -a && . .env && set +a
PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p 5432 -U "$DB_USER" -d "$DB_NAME"
```

**Run the DCAD ownership-history ingest (idempotent, memory-safe, ~25 min for 5 yrs):**
```bash
cd <worktree-with-the-script>
set -a && . /home/kk/projects/clients/lot-ledger/.env && set +a
python3 scripts/ownership_history/build_dcad_ownership_history.py \
  --historical-dir /home/kk/projects/clients/lot-ledger/ingest/counties/dallas/dcad/Historical
```
- Streams in bounded 50k batches (peak RSS ~52 MB) — safe on the dev box. Re-runnable.
- **DB-write discipline:** writes hit Mike's *prod* DB. Additive table changes are OK;
  anything touching existing tables → spec → preview-verify → KK sign-off first.

**Big-CSV caution:** never `pd.read_csv(whole_file, dtype=str)` on the DCAD CSVs — they're
huge and will thrash the 12 GB dev box into a freeze. Stream/chunk. (See memory
`dev-box-memory-constraint`.)

---

## 10. Worktree layout

```
/home/kk/projects/clients/lot-ledger        (primary; has .env, ingest/, data/)
/home/kk/projects/clients/lot-ledger-strip  (strip-runner work)
/home/kk/projects/clients/lot-ledger-ui     (feature UI work)
```
Create an isolated worktree for new feature work off the latest develop:
```bash
git worktree add /home/kk/projects/clients/lot-ledger-<name> -b feat/<name>-YYYY-MM-DD origin/develop
```

---

## 11. House rules (don't violate)

- **No `Co-Authored-By: Claude …` trailer** on commits or specs.
- **Never force-push `main`.** Never skip hooks / signing unless KK asks.
- Commit/push only when KK asks; if a message is ambiguous, draft + confirm.
- `docs/*` is gitignored except a few files (this runbook is an explicit exception); most
  specs/notes live only locally per-worktree.
