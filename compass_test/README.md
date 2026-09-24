# compass_test

Executes the Withdraw/Deposit hops the compass solver actually chose (the
same solved graph behind `graph.html` / `operations.txt`, see
`src/graph/solver.py`) against real DEX APIs and real on-chain wallets, then
compares the REAL gas paid and REAL elapsed time against that exact edge's
`Fee(e)`/`Time(e)` estimate (`src/graph/costing.py`). Results feed the
"Test Results" tab in the graph UI.

Scope: **Withdraw, Deposit, Swap and Bridge**, on **BSC and Arbitrum**
(plus **Solana** as the Bridge leg's destination, see below). Swap is a
same-chain USDC <-> USDT conversion executed through **CoW Swap**
(`runners/cowswap.py`, `connectors/cowswap.py`) — see "Swap hop" below.
Bridge is instrumented for both protocols `graph.structures.bridges` models:
Aden's own internal ledger (USDT, BSC<->Arbitrum, `runners/aden.py`) and
Circle's CCTP (USDC, ARBITRUM<->SOLANA — the withdraw pipeline's own exit
route, `connectors/cctp.py`, `cctp_runner.py`), picked automatically by
`hop_runner.py`/`plan_loader.py` from `availableBridgeProtocols`. A journey
whose Bridge leg falls outside what either protocol's connector actually
covers is still marked out of scope (`plan_loader.py`), never partially
executed.

## Imbalances are set by hand — and "Execute" runs the plan

The solver no longer draws random demo imbalances. Each DEX's surplus or
deficit is typed in the graph UI (click a DEX → "Imbalance" form → Save),
persisted in `connectors/dex_imbalances.json` (see
`connectors/dex_imbalances.py`), and "▶ Run solver" in the toolbar
rebuilds and solves the graph from exactly those numbers
(`main.buildAndSolveGraph`, `POST /api/recompute`). A surplus is an upper
bound (at most that much leaves the DEX), a deficit is filled exactly, so
the only feasibility rule is total surplus ≥ total deficit — checked before
the solver runs, with a readable message in the toolbar otherwise.

**Money already in the operating wallet counts too.** Each WalletNode
carries the wallet's real on-chain balance for that (chain, stable), read at
build time (`main._fetchLiveWalletBalances`) and offered to the solver as a
bounded source exactly like a DEX surplus (`WalletNode.balance`,
`solver._addFlowConservation`). So with +1000 on Aden, $3 in the Arbitrum
wallet and a $1 deficit on Lighter, the plan is one $1 deposit from the
wallet — not withdraw → bridge → deposit. Click a Wallet node to see what
the current plan was solved with, untick a stable to keep that money out of
the plan, or set a cap (`connectors/wallet_sources.json`, see
`connectors/wallet_sources.py`). A journey that starts at a wallet shows in
that Wallet node's Details panel, with its own Execute buttons.

Every Withdraw/Deposit hop the solver picked then gets an **Execute**
button (Chosen Operations tab, and the "Chosen by the solver" block of an
edge's Details panel) that runs that hop LIVE at the solver's amount
(`edge.flow`), through the same `POST /api/test-hop` route, typed-"YES"
confirmation and `COMPASS_TEST_ALLOW_LIVE` / `$` caps as "Run LIVE" below —
an amount above `COMPASS_TEST_MAX_USD_PER_HOP` is refused by the server and
shown as such. "Test This Edge" is unchanged: a small dry-run/live probe at
the configured minimum, independent of any plan.

## Test amounts

`cli.py run` and "Test This Edge" deliberately never use the solver's
amount: the solved flow is whatever imbalance was typed, which can exceed
any real account. They use a small floor instead:

**"Min withdraw" / "Min deposit" in the Config tab** (`DEX.minWithdrawUsdByChain`
/ `DEX.minDepositUsdByChain`, persisted in `connectors/dex_operational_params.json`
like the other operational params) are the fix: per-(DEX, chain) floors a real
withdrawal/deposit has to clear, editable the same way as withdraw/deposit
fees and delays. `cli.py run` defaults a journey's `--amount`, when omitted,
to:

```
max(destination DEX's minDepositUsd, source DEX's minWithdrawUsd)
```

— the smallest amount that clears BOTH ends of the route: below the source's
withdraw floor the withdrawal itself would be rejected; below the
destination's deposit floor the deposit wouldn't be credited even if the
withdrawal succeeded. Only MEXC's `minWithdrawUsd` entries are backed by a
live-fetched value right now (0.50 BSC / 1.00 Arbitrum, from the same
`capital/config/getall` call `runners/mexc.py` already makes — it exposes no
deposit-minimum equivalent, so `minDepositUsd` defaults to 0.0 everywhere,
meaning today the formula reduces to the source's min withdraw in practice).
Every other DEX starts at the same `DEFAULT_MIN_WITHDRAW_USD = 5.0` /
`DEFAULT_MIN_DEPOSIT_USD = 0.0` placeholders as the other operational-param
defaults, meant to be edited once you have the real numbers for that venue.

## Balance readers (`balances.py`)

Separate from the withdraw/deposit connectors in `runners/` (which require
`withdraw()` and `build_deposit_tx()`, a much higher bar than a plain
balance read): `balances.py` is a read-only `get_real_balance_usd(dexName)`
per DEX, calling each DEX's own API directly with the credentials already in
`piggybank-arb/.env` — no zfund, no sentinel calls at runtime. All 8
registry DEXes have a working reader today:

| DEX | Endpoint | Auth |
|---|---|---|
| MEXC | `GET /api/v3/account` | HMAC-SHA256 (via `runners/mexc.py`) |
| Lighter | `GET /api/v1/account` | none — public read |
| Hyperliquid | `POST /info` (`spotClearinghouseState` + builder-dex `clearinghouseState`) | none — public read, matches piggybank-arb's own `fetchBalanceFromRest` formula exactly |
| dYdX | `GET /v4/addresses/{address}` | none — public indexer read |
| Extended | `GET /api/v1/user/balance` | API key only (`X-Api-Key`) — no Stark signature needed for reads |
| Aster | `GET /fapi/v3/account` | keccak/ABI-encode/personal_sign — see below |
| Aden | `GET /api/v1/dex_futures/usdt/accounts` | HMAC-SHA512, Gate.io-style (`KEY`/`Timestamp`/`SIGN` headers) |
| Ondo Perps | `GET /v1/perps/balance` | HMAC-SHA256 (`ONDO-KEY-ID`/`ONDO-TIMESTAMP`/`ONDO-SIGN` headers) |

**Aster, Aden and Ondo Perps were NOT figured out from public docs** — each
DEX's public documentation is either wrong (Aster), silent on the
authenticated endpoint entirely (Aden), or describes a different,
more-involved flow than what a provisioned API key actually uses in
production (Ondo's Bearer-JWT-via-SIWE guide). All three were ported
directly from `sentinelBackend/sentinel/src/connectors/{aster,aden,ondo}/http.py`
— an existing, already-working balance-streaming service for these same
accounts — rather than guessed or re-derived. See each function's docstring
in `balances.py` for the exact source it mirrors.

**Update**: the Aster fix has since been ported into `runners/aster.py` too
(`aster_signing.py` is now the one shared implementation both modules call —
see its docstring). `check-auth --dex Aster` and `poll_balance_usd` are
confirmed working through the real `AsterConnector` class, not just
`balances.py`. The `withdraw()` path's REST auth is fixed the same way; the
separate EIP-712 "Action" struct that authorizes the withdrawal itself
(distinct concern, see `runners/aster.py::_withdraw_action_signature`) is
still best-effort and unverified live — see Connector status below.

## Setup

```bash
.venv/bin/pip install -r compass_test/requirements.txt   # adds web3 on top of compass's own requirements.txt
```

Secrets are **not** copied into this folder. `config.py` loads compass's own
`.env` (for `ALCHEMY_API_KEY` — same gas/USD-price source `costing.py` itself
uses) plus `piggybank-arb/.env` directly by path (its DEX credentials). Set
`PIGGYBANK_ARB_ENV_PATH` if that repo lives somewhere other than
`../piggybank-arb` relative to `compass/`.

Add to `compass/.env`:

```bash
# Which env var (from either .env above) holds the operating wallet's
# private key — see "Operating wallet" below. Only needed for a LIVE
# deposit or swap.
COMPASS_TEST_WALLET_KEY_VAR=

# Swap hop (CoW Swap) knobs — optional, see "Swap hop" below:
COMPASS_TEST_SWAP_TEST_USD=1          # test amount when --amount is omitted
COMPASS_TEST_SWAP_SLIPPAGE_BPS=50     # min buy amount = quote − this

# The same wallet's public address — enough on its own for `list-hops` /
# dry runs, no private key material needed on the machine for those.
COMPASS_TEST_WALLET_ADDRESS=

# Both required, independently, for ANY live transfer (belt-and-suspenders —
# see cli.py's --live flag too):
COMPASS_TEST_ALLOW_LIVE=0

# Hard caps enforced regardless of the above:
COMPASS_TEST_MAX_USD_PER_HOP=10
COMPASS_TEST_MAX_USD_PER_RUN=20
```

## Operating wallet

Deposits are on-chain: something has to sign and pay gas for the ERC-20
transfer (or a DEX's own deposit-contract call). `piggybank-arb/.env` has
several DEX-specific keys (`ASTER_PRIVATE_KEY`, `HYPERLIQUID_API_ADDRESS`,
...) that authenticate *trading*, not necessarily a general-purpose wallet —
picking the wrong one risks signing with a key whose address the target DEX
won't recognize as a depositor. `COMPASS_TEST_WALLET_KEY_VAR` is deliberately
an indirection: set it to the NAME of whichever env var is actually a usable
general wallet key for you (e.g. `COMPASS_TEST_WALLET_KEY_VAR=ASTER_PRIVATE_KEY`),
never the secret itself.

One exception: **Aster's connector does not use this** — `depositFor` takes
an explicit `forAddress` parameter, so Aster credits `ASTER_USER` regardless
of which wallet actually pays the gas. The generic operating wallet only
needs to be *some* funded EVM wallet for Aster's deposit leg.

## What we measure

Withdraw and Deposit are asymmetric — see `executor.py`:

- **Withdraw** (DEX → our wallet): the exchange pays its own gas; we only
  see a flat fee. `actualCostUsd = amountRequested − amountReceivedOnChain`
  (polled via the same Alchemy RPC `costing.py` uses), `actualTimeSeconds`
  = time from request accepted to the wallet balance actually increasing.
  **Live-only** — an exchange's processing time/fee can't be simulated, so a
  dry run reports `status="dry_run"` with no number.
- **Deposit** (our wallet → DEX): we pay real gas.
  `actualCostUsd = gasUsed × effectiveGasPrice`, priced in USD at
  confirmation time via `AlchemyConnector.get_usd_price` — the same price
  source `costing.py` itself uses, so the comparison isn't biased by a
  different feed. A **dry run still gets a real number here**:
  `eth_estimateGas` simulates against live chain state without signing or
  broadcasting anything.

- **Swap** (our wallet, stable A → stable B on the same chain, via CoW
  Swap): the wallet pays no gas for the trade itself — CoW's winning solver
  does, and recovers it through a network fee taken out of the sell amount.
  So `actualCostUsd = amountSold − amountBought` (that fee plus the price
  impact, all-in, from the orderbook's `executedSellAmount`/
  `executedBuyAmount`, cross-checked against the wallet's real on-chain
  balance of the bought stable), **plus** the gas of the one-off ERC-20
  `approve` to CoW's vault relayer when one was needed (exact-amount
  approval, never unlimited). `actualTimeSeconds` = order posted →
  settled on-chain (CoW is a batch auction: tens of seconds, not one
  block). The estimate it's compared against is what the solver charges
  for that same edge at that amount: the fixed swap gas `Fee(e)`
  (`costing.computeCost`) + the Uniswap-QuoterV2 slippage
  (`costing.computeRealizedSwapSlippageUsd`). A **dry run gets a real
  number here too**: a live CoW quote prices exactly `sold − bought`
  without signing anything (plus the simulated approve gas if the
  allowance is missing).

Hop-level comparisons roll up into per-journey totals
(`JourneyComparison`, mirroring `visualization/journeys.Journey` — "Aster →
MEXC", not individual edges), matching how the graph UI already presents
chosen operations.

## Swap hop (CoW Swap)

`runners/cowswap.py` drives `connectors/cowswap.py` through the full
off-chain order lifecycle: **quote** (`POST /{bnb|arbitrum_one}/api/v1/quote`,
`kind: sell`, `sellAmountBeforeFee` = the test amount so the `$` caps bound
what can leave the wallet) → **allowance check** on the sell token for
CoW's vault relayer `0xC92E8bdf79f0507f65a392b0ab4667716BFE0110`, with an
exact-amount `approve` tx signed and sent by the operating wallet when
short → **order**: `feeAmount 0`, `sellAmount` = quoted sell + fee (the
fee-in-surplus model), `buyAmount` = quoted buy − `COMPASS_TEST_SWAP_SLIPPAGE_BPS`
(default 50 bps; CoW never fills below it, a worse market just lets the
order expire) → **EIP-712 signature** by the operating wallet (domain
`Gnosis Protocol`/`v2`/settlement `0x9008D19f58AAbD9eD0D60971565AA8510560ab41`,
the `Order` struct of `GPv2Order.sol`) → `POST /orders` → **poll**
`GET /orders/{uid}` until `fulfilled` (settlement `txHash` from
`GET /trades`) → on timeout, the still-open order is **cancelled** (signed
`OrderCancellations`) rather than left to settle later unobserved.

Verified live 2026-09-10 without moving funds: quotes on both chains
(`verified: true`, ~$0.01 network fee on $1); the EIP-712 digest
`eth_account` computes equals a hand-rolled `keccak(TYPE_HASH ‖ abi.encode)`
of the contract's struct (`tests/test_cowswap_order.py`); and the real
orderbook recovered this wallet as signer for a deliberately unfillable
limit order (2 USDT per USDC, `POST /orders` → 201), then accepted its
cancellation (`open` → `cancelled`, executed amounts 0). One finding worth
knowing: **the orderbook does not check the sell-token allowance at
submission** — an unapproved order is accepted and simply sits `open`
until it expires, which is why the runner sets the approval itself before
posting. A real filled swap has **not** been run yet — see Connector status.

The swap has no DEX: reports carry the venue name `CoW Swap` in the `dex`
slot, `stable` is the stable sold and `toStable` the one bought
(`PlannedHop.toStable`). A live `ok` swap becomes a measured delay like any
other hop (`connectors/dex_measured_delays.json` under `"CoW Swap"` /
`swapDelaySeconds`, per chain) that `costing.computeDelay` then prefers
over the block delay it otherwise assumes for a Swap edge.

## Measured delays feed back into the solver

The `withdrawDelaySeconds` / `depositDelaySeconds` typed into the Config tab
are what each DEX's own frontend claims ("withdraw in 5 min"), and the live
runs above showed them to be off by up to an order of magnitude in both
directions. So they are now only a **fallback**: every live hop with
`status="ok"` is a delay sample, and `calibration.py` averages the last 10
samples per (DEX, hop type, chain) into `connectors/dex_measured_delays.json`,
which `connectors/dex_measured_delays.py` loads back onto the DEX
(`DEX.measuredWithdrawDelayByChain` / `measuredDepositDelayByChain`) and
`costing.computeDelay` prefers over the configured value:

- **one** successful live run already overrides the config (1 real
  measurement beats the claim), two are averaged, and so on up to the 10
  most recent — older samples drop out;
- a (DEX, chain, direction) with **no** successful live run keeps its
  configured / `DEFAULT_*` delay, untouched;
- for a deposit the sample is the full `startedAt -> finishedAt` delta
  (tx built, sent, confirmed, then the DEX balance seen increasing), so it
  already contains the on-chain confirmation — the chain block delay is
  **not** added on top of a measured value (it still is on top of a
  configured one);
- `unconfirmed`, `error` and `dry_run` hops carry no completed delay and
  are ignored;
- the config file is never rewritten — the typed value stays visible
  (struck through) next to the measured mean, n and std in the Config tab,
  and `PlannedHop.configuredTimeSeconds` / `timeSource` keep it in every
  report so the Test Results tab still shows how wrong the claim was.

The JSON is rebuilt from **all** reports on disk after every live run
(`reporter.save_report`) and on demand:

```bash
python -m compass_test.cli calibrate-delays
```

which also prints configured vs. measured per hop. Deleting a bad report
from `reports/` and re-running that command is enough to drop its sample.
The in-memory graph behind the UI picks the new values up at the next build
(`Recompute routes` / server restart), exactly like a config edit.

`POLL_INTERVAL_SECONDS` defaults to 2s (was 5s) since it bounds how much a
measurement overshoots the real credit time — noticeable next to a ~3s
Arbitrum hop, now that those measurements drive `Time(e)`.

## Metrics dashboard (optional, dev-only)

The "Rebalancings Tracker" page (`/metrics` on the graph viewer server)
charts cost/delay history across runs. It reads from a local TimescaleDB
container, not the JSON reports directly — those files under `reports/`
stay the durable source of truth either way. If the container isn't
running, that one page shows a "Metrics unavailable" banner; nothing else
(the graph viewer, report saving, live execution) is affected — see
`reporter.py`'s best-effort write, which only warns on failure.

To bring it up:

```bash
docker compose up -d          # starts compass_timescaledb on localhost:5433
                               # (see docker-compose.yml — 5433 to avoid
                               # clashing with any local Postgres on 5432)
```

The schema (`sql/schema.sql`) applies automatically on the container's
first boot. To populate it from reports already on disk (not needed for a
fresh setup going forward — `reporter.py` writes new runs to it live):

```bash
python -m compass_test.scripts.backfill_metrics_db
```

Connection string defaults to
`postgresql://compass:compass_dev_only@localhost:5433/compass_metrics`
(`config.py`'s `TIMESCALE_DB_URL`, overridable via the same-named env var).

## Usage

```bash
# What would be tested, and why some journeys aren't (no connector, or a
# Swap/Bridge hop):
python -m compass_test.cli list-hops

# Smoke-test a connector's auth with a read-only balance call — no funds
# moved. Do this BEFORE the first live run of a DEX, especially Aster (see
# "Connector status" below):
python -m compass_test.cli check-auth --dex MEXC --stable USDT

# Dry run (default) — real gas/quote data, nothing signed or sent. --amount
# is optional: omitted, it uses the source DEX's configured minimum
# withdraw amount (Config tab "Min withdraw" — see below) instead of an
# arbitrary number, since the solved flow is whatever imbalance was typed
# in the UI and can be far larger than any real account balance:
python -m compass_test.cli run --from Aster --to MEXC

# Live — moves real money. Requires COMPASS_TEST_ALLOW_LIVE=1 in the
# environment AND --live here; without --yes you'll see a summary and have
# to type YES to proceed:
python -m compass_test.cli run --from Aster --to MEXC --amount 5 --live

# Swap ONE stable for the other on the same chain through CoW Swap (no
# --dex: a swap has no DEX). Dry run = a real orderbook quote; --live
# approves (if needed), signs, posts and waits for the order to settle:
python -m compass_test.cli run-hop --hop swap --chain ARBITRUM --stable USDC --to-stable USDT --amount 1
python -m compass_test.cli run-hop --hop swap --chain BSC --stable USDT --to-stable USDC --amount 1 --live
```

## Safety model

- Dry-run by default, both from the CLI and the frontend. A transfer only
  happens with explicit opt-in from whichever surface is used:
  - **CLI**: `--live` on the command line **and** `COMPASS_TEST_ALLOW_LIVE=1`
    already set in the environment — a stray flag alone is never enough
    (`config.py`).
  - **Frontend** (graph UI's "Test This Edge" → "Run LIVE" button, see
    `visualization/server.py` `POST /api/test-hop`): the SAME
    `COMPASS_TEST_ALLOW_LIVE=1` server-side gate, **plus** a `confirm: "YES"`
    the request body must carry — checked before `compass_test` is touched
    at all, so hitting the endpoint directly (curl, a script) still needs to
    know and send it, not just a browser click.
- Hard `$` caps per hop and per run (`executor._check_caps`), enforced
  regardless of `--live`/`--yes` or the frontend's `live`/`confirm` — one
  check, both surfaces go through it (`executor.run_hop`).
- An interactive typed `YES` confirmation before a live run: a terminal
  prompt for the CLI (skippable with `--yes`), a text field the frontend
  requires to literally read "YES" before its "Run LIVE" control is even
  clickable.
- **Every actual execution path funnels through one function**
  (`hop_runner.run_single_hop`) **and one safety gate**
  (`executor.run_hop`/`_check_caps`) — the CLI and the frontend are two thin
  callers of the same code, not two independent (and possibly
  inconsistently gated) implementations.

## Connector status

**Every DEX in the active registry (`src/graph/structures/dex_registry.py`)
now has both a Withdraw and a Deposit implementation** — Aden, Aster,
Extended, Hyperliquid, Lighter, MEXC, Ondo Perps. "Implemented" and
"exercised against a real live transaction" are still two different things
per-leg below — several rows are code-complete but only smoke-tested
(`check-auth`) or verified up to (not including) the final submit call, see
each row's own notes and "Verify before your first live run" further down.
dYdX and Gate (Perp DEX) are commented OUT of the registry entirely
(`dex_registry.py`), not present in the live graph at all — not a gap in
"every DEX," since they aren't one of the DEXes the solver ever routes
through today. dYdX's own reason stays below for when it's reconsidered:
a real withdraw is three hops and only the first has working vendor SDK
support.

| DEX | Withdraw | Deposit | Notes |
|---|---|---|---|
| MEXC | ✅ | ✅ | Standard REST+HMAC, verified against MEXC's public docs (2026-09-04). Smoke-tested live via `check-auth` against the real account — works. |
| Aster | ✅ (REST auth fixed; withdraw-authorization struct unverified live) | ✅ (untested live) | Deposit is a documented vault `depositFor` call, not yet exercised live. `check-auth --dex Aster` passes (`$0.00`, real). Request-level auth is fixed (`aster_signing.py`, shared with `balances.py`). One real unknown remains: the SEPARATE EIP-712 "Action" struct that authorizes the withdrawal amount/destination itself (`_withdraw_action_signature`) is still a best-effort field reconstruction from Aster's docs, never exercised against a real withdraw. Fails closed if wrong (Aster rejects the signature, no funds move) — but **do not attempt a live Aster withdraw as a first test of this.** |
| Ondo Perps | ✅ (confirmed live) | ✅ (confirmed live) | The earlier "no documented withdraw, deposit needs a Bearer JWT" verdict was based on the "Builder Integration Guide" page alone; the separate `docs.ondoperps.xyz/api-reference/wallet/*` pages document both `POST /v1/withdraw` and `POST /v1/provision_address` against the same `ONDO_API_KEY`/`ONDO_API_SECRET` pair `balances.py::_ondo` already uses (ONDO-KEY-ID/ONDO-TIMESTAMP/ONDO-SIGN, not the docs' bare `X-API-KEY-ID` name). **Deposit confirmed live 2026-09-07**: a real $1.00 Arbitrum-USDC transfer to a freshly `provision_address`'d address, credited (`poll_balance_usd` $2.12→$3.12), tx `0x822f9ece4759e02655f9cd5e3a21fa524d13e0d491e5f980f185d544aa668b9e`. Arbitrum isn't in any api-reference page's documented `network` enum (only `ethereum`/`avalanche`/`solana` are) but works — this account's deposit addresses turn out to be shared across all three EVM networks Ondo watches, not per-network vaults. **Withdraw confirmed live 2026-09-07** ($1.50, `externalId f12fbd50...`) after fixing a real bug hit along the way: the first attempt 400'd `withdrawal_address_not_found` for this account's own wallet address even though it WAS in the address book — the book stores it lowercase, `wallet.address` is EIP-55 checksummed, and Ondo's lookup doesn't normalize case before comparing (`runners/ondo.py::_address_book_entry` now looks it up case-insensitively and sends back whatever string is actually on file). `executor.py`'s before/after on-chain balance poll (not just the REST "pending" response) confirmed the wallet's real balance rose by the full $1.50 — `actualCostUsd: 0.0`, i.e. `withdrawalFeeUSD`'s reported "$1" was NOT actually deducted this time, worth re-checking on a larger withdrawal. See `runners/ondo.py` docstring for the full trail. |
| Aden | ✅ **confirmed live** (2026-09-07) | ✅ **confirmed live** (2026-09-07) | **BSC only.** Reverse-engineered live 2026-09-07 by capturing the real web app's own network + wallet-signing traffic (see `runners/aden.py` docstring) — withdraw/login are on a completely separate host pair (`perps.aden.io` / `brokerapi.gateperps.com`) from the HMAC-keyed `api.aden.io` `balances.py::_aden` already uses. Login is fully headless (`_login()` does a `personal_sign` over Aden's challenge and mints its own `perp_evm_access_token`). Deposit: a plain ERC-20 transfer to Aden's own "Deposit" screen address — **confirmed live: a real deposit landed and was credited.** Withdraw: **confirmed live** — a real $10.50 BSC withdraw, `$10.30` received ($0.20 fee, matching config), real on-chain balance increase verified. Getting there took a real HAR capture of a successful browser withdrawal to find two independent bugs that had been masking each other behind a generic `P_FOMOX_IN_INTERNAL_ERROR` 500: (1) `x-perp-broker-main-uid` was sent as the wrong uid (`perp_evm_uid` instead of `perp_main_uid` — two separate fields this connector had conflated), and (2) the `Authorization: Bearer <general access_token>` header, wrongly removed in an earlier fix attempt that reasoned from the HAR alone (which doesn't show browser session cookies) — both had to be fixed together, since either one alone still 401'd. Nine other single-field guesses (casing, amount formatting, explicit nonce, extra headers) were tried and ruled out before the real capture settled it — see `runners/aden.py`'s docstring for the full trail. `_check_min_withdraw` (a separate, earlier-fixed bug: the real minimum is $10.20 BSC / $10.50 Arbitrum) remains in place. |
| Gate (Perp DEX) | ❌ | ❌ | Excluded for this v1 by explicit request. |
| Extended | ✅ (untested live — real reads/quote/signature confirmed, final submit not attempted, see notes) | ✅ (implemented, untested live) | Checked directly against `api.docs.extended.exchange` and the real `x10-python-trading-starknet` PyPI package (2026-09-06) — Extended is StarkEx-derived, so every write (including withdraw) needs a STARK-curve signature, not the plain API key reads use. Withdraw is a 4-step Rhino.fi bridge flow to Arbitrum, documented on Extended's own side (`GET /user/bridge/config` → `GET /user/bridge/quote` → `POST /user/bridge/quote` commit → `POST /user/withdrawal`, STARK-signed); the signature itself uses `fast_stark_crypto`, the same Rust-backed library Extended's own SDK calls, run in a **dedicated Python 3.9 venv** (`runners/extended_signers/`) since it has no Python 3.14 wheel and refuses to build for one at all — confirmed by trying, not assumed. Verified live: the derived public key from `EXTENDED_STARK_PRIVATE_KEY` matches this account's own `l2Key` from a real `GET /user/account/info` call exactly, and a real `GET /user/bridge/quote` + a real signed settlement object were both produced successfully (fee: $0.01 on a $5 test quote). The final `POST /user/withdrawal` was deliberately not called — this account's real balance ($0.44) is below any sensible test amount. **Deposit is now implemented** (`build_deposit_tx`, same Rhino.fi bridge as the withdraw leg — `GET /user/bridge/config`/`quote`, committed via `POST /user/bridge/quote`, then `depositWithId` on the bridge contract, ABI confirmed against Rhino.fi's own reference docs, approve-first if allowance is short) — this row and `runners/registry.py`'s own comment (which still said "build_deposit_tx raises") were out of sync until this update; neither leg's final on-chain call has been run against a real amount yet. |
| dYdX | ❌ | ❌ | Checked directly against `docs.dydx.xyz` and the real `dydx-v4-client` PyPI package (2026-09-06) — a withdraw here is THREE hops, not one. Hop 1 (subaccount → your own dYdX main account) is solid: `NodeClient.withdraw()` really does sign+broadcast a `MsgWithdrawFromSubaccount`. Hop 2 (dYdX main account → Noble, IBC) has no working vendor implementation — the SDK's own `NobleClient.send_token_ibc()` is unfinished as published (computes `coin = token.coin()` and the function just ends, no message, no return). Hop 3 (Noble → Arbitrum, Circle's CCTP `MsgDepositForBurn`) isn't in the SDK at all. Hand-rolling hops 2/3 means an unverified CCTP burn call with no reference implementation — get the destination-domain/recipient-padding wrong and the USDC burns with nothing minting on the other end, irreversibly. The real alternative, Skip Go (dYdX's own documented bridge partner, real public API at `api.skip.build`, confirmed reachable), returns pre-built messages for the whole route instead — but needs a new Cosmos mnemonic credential and real protobuf tx signing against two chains, decided against building for now. **Balance reading works** (`balances.py`), no signing needed. |
| Lighter | ✅ (untested live) | ✅ (implemented, untested live) | Withdraw is a "fast withdraw" (L2 transfer to a pool account, destination address riding in the memo), signed via the same compiled Go binary `lighter-sdk` vendors internally (see `runners/lighter_signers/README.md`). Deposit is a "fast deposit" via Circle's CCTP bridge-intent-address flow (`apidocs.lighter.xyz` "CCTP Method", chosen over Lighter's other two documented deposit paths — see `runners/lighter.py`'s own docstring), no auth needed for the CCTP leg itself; $5.00 minimum (the CCTP path's own floor). This is a SEPARATE CCTP integration from the Arbitrum->Solana withdraw pipeline's (`connectors/cctp.py`) — same protocol, different destination/purpose, not shared code. The "secure" (non-fast) on-chain withdrawal path was not built. |
| **CoW Swap** (Swap hop, USDC<->USDT on BSC + Arbitrum) | n/a | n/a | Swap only — see "Swap hop" above. Quote/sign/submit/cancel all **confirmed against the live orderbook 2026-09-10** with this wallet (unfillable test order accepted then cancelled, nothing executed); dry runs return real quotes on both chains. **A real filled swap has not been run yet** — first live test should be `run-hop --hop swap --chain ARBITRUM --stable USDC --to-stable USDT --amount 1 --live` (USDC is already approved to the vault relayer there, so no approve tx), then the BSC direction (needs one approve tx first, ~$0.002 gas). |
| Hyperliquid | ✅ (untested live) | ✅ (untested live) | Both documented directly (`hyperliquid.gitbook.io`, fetched 2026-09-06) — no reverse-engineering needed, unlike Aster. Deposit is a plain USDC transfer to Hyperliquid's own fixed Arbitrum bridge contract (`0x2Df1c51e09aecf9cacb7bc98cb1742757f163dF7`, verified against `github.com/hyperliquid-dex/contracts/Bridge2.sol`); **credited to whichever address sends it** (no `forAddress` override like Aster's vault), and **below the $5 minimum is lost, not credited** — both enforced in `build_deposit_tx` before broadcasting. Withdraw is a single EIP-712 `"withdraw3"` user-signed action (`for-developers/api/exchange-endpoint.md`) submitted straight to `/exchange` — no separate on-chain tx. Neither leg has been exercised live: `check-auth` only proves the read side works, not that a real bridge deposit lands or a real withdraw clears. The signer (and deposit sender) is required to BE the account itself (`HYPERLIQUID_WALLET_ADDRESS`) — `runners/hyperliquid.py` refuses outright if the configured operating wallet is any other address, since Hyperliquid's delegated API/agent credentials (`HYPERLIQUID_API_KEY`/`HYPERLIQUID_API_ADDRESS`) are a trading-only key, the same situation as Aster's `ASTER_SIGNER`. |

`compass_test/runners/unsupported.py` is the single source of truth for
these reasons; `TestRunReport.unsupportedDexes` (and the frontend's Test
Results tab) surface them on every run rather than silently omitting them.

## Verify before your first live run

- **Aster vault contract addresses** (`runners/aster.py`) — cross-checked
  against two independent fetches of Aster's own docs, byte-for-byte
  identical, but still re-verify on Arbiscan/BscScan before trusting a real
  deposit.
- **Aster EIP-712 withdraw struct** — best-effort field reconstruction;
  `check-auth` validates the REST auth half cheaply, but the actual
  withdraw signature is only proven correct by Aster's server accepting it.
- **MEXC network name resolution** — resolved live against
  `/api/v3/capital/config/getall` rather than a hardcoded string, but the
  substring match (`runners/mexc.py:_NETWORK_HINT_BY_CHAIN`) is worth a
  glance the first time you add a new chain.
- **Aden withdraw, first live test** — the REST auth (headless `_login()`)
  and the deposit path are both confirmed live; the withdraw `sign_data ->
  sign -> submit` sequence itself has not been. Worth a small live withdraw
  as its own first test rather than assuming it works because the pieces
  around it do. `ADEN_DEVICE_FINGERPRINT` in `piggybank-arb/.env` is a
  stable per-installation id, not a short-lived token — no manual refresh
  expected, but if login ever starts failing, re-copy it from a real
  session's `localStorage['aden_web3_device_finger_key']` (see
  `runners/aden.py`'s docstring for the full recipe).

## Tests

```bash
PYTHONPATH=src .venv/bin/pytest compass_test/tests -v
```

Pure comparator/safety-cap math — no network calls, no keys required.
