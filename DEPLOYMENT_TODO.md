# Deployment TODO — Compass frontend/server

For whoever deploys `visualization/server.py` (the Flask app behind
`graph.html`) somewhere other than a developer's own laptop. Written from
auditing the current code directly (2026-09-23) — every item below reflects
what's actually there today, not generic advice. Nothing here has been done
yet; this is the gap list, not a status report.

**Read this before starting**: the server can execute *real* trades and
on-chain transfers (`POST /api/test-hop`, `Execute` buttons) using private
keys it reads from `.env`. Get the auth and secrets sections done before
this is reachable from anywhere but `localhost`.

## 0. Blocking — do these first, in this order

- [ ] **Add authentication.** Right now there is none — every route on
  every endpoint (including the ones that move real funds) is open to
  anyone who can reach the port. It's currently bound to `127.0.0.1` only,
  which is the only thing keeping this safe today. Do not change the bind
  address (see next item) until this is done.
- [ ] **Decide how this gets reached** and change the bind accordingly.
  `app.run(host="127.0.0.1", ...)` is hardcoded in `visualization/server.py`
  (bottom of the file, inside `main()`). Options, cheapest/safest first:
  a private VPN/Tailscale network (no code change needed, just don't open
  the port publicly), a reverse proxy (nginx/Caddy) on the same box that
  adds auth and TLS in front of it, or changing the bind to `0.0.0.0` +
  a firewall rule — only do the last one once auth is confirmed working.
- [ ] **Rotate the Solana wallet key.** `COMPASS_TEST_SOLANA_WALLET_PRIVATE_KEY`
  in `.env` was pasted into a chat conversation at some point — treat it as
  compromised. Generate a fresh keypair, move any funds over, update `.env`
  and `COMPASS_TEST_SOLANA_WALLET_ADDRESS`, before this goes anywhere near
  production traffic.

## 1. Run it as an actual service, not a foreground script

- [ ] Flask's built-in server (what `app.run()` uses) is explicitly not
  meant for production — no process supervision, one Python process. Put
  it behind a real WSGI server: `gunicorn -w 1 --threads 8
  'visualization.server:app'` (keep `-w 1`: the app holds an in-memory
  solved `Graph` as module state — see `main()`'s own comment about
  `threaded=True` — so multiple worker *processes* would each solve/hold a
  different graph; more *threads* is fine, more *processes* isn't, unless
  that in-memory state gets moved out first). No `gunicorn`/`uwsgi`/
  `waitress` is in `requirements.txt` yet — add whichever you pick.
- [ ] Add process supervision (systemd unit, supervisor, or your platform's
  equivalent) so it restarts on crash and on boot. Nothing like this
  exists in the repo today.
- [ ] Decide whether `--mode prod` (hides "Test This Edge", keeps only
  "Execute" — see `visualization/server.py main()` and the README section
  this was added for) is what you want for this deployment's audience.

## 2. Secrets

Everything below is currently a plaintext `.env` file loaded via
`python-dotenv` (`connectors/config.py`, `compass_test/config.py`) — fine
for a laptop, not for a deploy target. Move these into whatever secrets
manager your platform provides (env vars injected by the platform, a vault,
etc.) rather than shipping the `.env` file itself:

- `ALCHEMY_API_KEY` — gas prices / USD conversion, all chains.
- `COMPASS_TEST_WALLET_PRIVATE_KEY` — the EVM operating wallet. Real funds.
- `COMPASS_TEST_SOLANA_WALLET_PRIVATE_KEY` — the Solana wallet (see rotation
  item above). Real funds.
- `ASTER_PRIVATE_KEY` / `ASTER_SIGNER`.
- Whatever's in `piggybank-arb/.env` too (`PIGGYBANK_ARB_ENV_PATH` in
  `compass_test/config.py`) — MEXC/Aden/other DEX API keys. That's a
  *separate* repo/`.env` this one reads by path; confirm it's deployed
  somewhere this server can still reach, or copy its contents into
  whatever secrets store you land on.
- `TIMESCALE_DB_URL` currently defaults to a hardcoded dev password
  (`compass_dev_only`, see `docker-compose.yml`) — set a real one if you
  stand up TimescaleDB for real (see §4).

- [ ] Also decide `COMPASS_TEST_ALLOW_LIVE`, `COMPASS_TEST_MAX_USD_PER_HOP`,
  `COMPASS_TEST_MAX_USD_PER_RUN` deliberately for this deployment's real
  risk tolerance — don't just carry over whatever was set on a dev machine.

## 3. TLS

If this is reachable from anywhere but `localhost` or a private network,
it needs HTTPS — private keys and live trade requests should not go over
plain HTTP. Typically the reverse proxy from §0 terminates this
(Let's Encrypt via Caddy/nginx/certbot); no TLS handling exists in the
Flask app itself.

## 4. Data the server depends on

- [ ] `graph.html` / `graph.png` / `operations.txt` at the repo root are
  **generated files** (`src/main.py`, or the server's own
  `POST /api/recompute`) — whatever's currently committed is a snapshot
  from whenever it was last run, not live. Make sure your deploy process
  either runs `python src/main.py` once at startup, or that the first
  thing an operator does is hit "Run solver" in the UI, so the served page
  isn't showing stale data from a dev machine.
- [ ] `connectors/dex_imbalances.json`, `connectors/wallet_sources.json`,
  `connectors/wallet_deficits.json`, `connectors/dex_operational_params.json`
  are hand-edited config files read/written by the server — decide where
  these live persistently (a mounted volume, not the container filesystem,
  if this runs in a container) so edits survive a restart/redeploy.
- [ ] `compass_test/reports/*.json` — the durable source of truth for every
  test/live run (TimescaleDB, if used, is only an additive projection of
  these, see `compass_test/metrics_db.py`'s own comment). Same persistence
  question as above.
- [ ] TimescaleDB (`docker-compose.yml`) is optional — the app works
  without it, only the `/metrics` dashboard goes dark (confirmed: it's
  not currently running even in dev, server just logs a connection-refused
  warning and moves on). Decide if you're standing it up for this
  deployment; if so, it needs its own backup story, not just
  `docker compose up -d` and forget.

## 5. Observability

Nothing exists here today beyond stdout logging from Flask/gunicorn:

- [ ] Ship logs somewhere durable (the platform's log aggregation, or
  redirect stdout/stderr to a file that's actually rotated/retained).
- [ ] Add an uptime/health check — there's no dedicated `/health` route;
  `GET /api/execution-status` is a reasonable cheap stand-in if you need
  one now rather than adding a real one.
- [ ] Consider alerting on a live hop's `status="error"`/`"unconfirmed"`
  outcome — right now nothing pages anyone if a live transfer fails or
  stalls mid-flight. Especially relevant for the CCTP withdraw pipeline
  (`compass_test/cctp_runner.py`), which blocks 13-19 minutes waiting on
  Circle's attestation with no crash-recovery if the process dies in that
  window — flagged as a known gap, not something this deployment pass
  fixes, but worth knowing before relying on it unattended.

## 6. Before flipping it on for real

- [ ] Confirm `git status` is clean and whatever's deployed matches a
  specific pushed commit, not an ad-hoc working tree.
- [ ] Smoke-test with `COMPASS_TEST_ALLOW_LIVE` unset/0 first — confirm the
  UI loads, dry-run hops work, before enabling live at all.
- [ ] Re-run the full test suite (`pytest`, repo root) against the deployed
  environment's Python version/dependencies, not just locally.
