# compass_test

Executes the Withdraw/Deposit hops the compass solver actually chose (the
same solved graph behind `graph.html` / `operations.txt`, see
`src/graph/solver.py`) against real DEX APIs and real on-chain wallets, then
compares the REAL gas paid and REAL elapsed time against that exact edge's
`Fee(e)`/`Time(e)` estimate (`src/graph/costing.py`). Results feed the
"Test Results" tab in the graph UI.

Scope for this v1 (see the plan this was built from): **Withdraw and Deposit
only**, on **BSC and Arbitrum**. No Swap, no Bridge — a journey that touches
either is marked out of scope wholesale (`plan_loader.py`), never partially
executed.

## Test amounts vs. the demo graph's imbalances

`main._generateRandomImbalances` (the demo data behind `graph.html` /
`operations.txt`) draws $500–$3000 surpluses/deficits per DEX — placeholder
numbers for visualizing the solver, not real account sizes. Real balances
(see sentinelBackend/sentinel, which streams each DEX's actual authenticated
`equity`/`balance`/`available` — `docs/redis-schema.md`'s `Balance` row) are a
different order of magnitude: e.g. the live MEXC account this session checked
via `check-auth` held $42.74, and MEXC's own live
`/api/v3/capital/config/getall` reports a $0.50 minimum USDT withdrawal on
BSC / $1 on Arbitrum. Testing with the graph's solved-flow amount (`solvedFlowUsd`
on a hop) would mean requesting a withdrawal for far more than any real
account holds.

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
# private key — see "Operating wallet" below. Only needed for a LIVE deposit.
COMPASS_TEST_WALLET_KEY_VAR=

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

Hop-level comparisons roll up into per-journey totals
(`JourneyComparison`, mirroring `visualization/journeys.Journey` — "Aster →
MEXC", not individual edges), matching how the graph UI already presents
chosen operations.

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
# arbitrary number, since the graph's own demo imbalances (see
# main._generateRandomImbalances) are typically far larger than any real
# account balance:
python -m compass_test.cli run --from Aster --to MEXC

# Live — moves real money. Requires COMPASS_TEST_ALLOW_LIVE=1 in the
# environment AND --live here; without --yes you'll see a summary and have
# to type YES to proceed:
python -m compass_test.cli run --from Aster --to MEXC --amount 5 --live
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

| DEX | Withdraw | Deposit | Notes |
|---|---|---|---|
| MEXC | ✅ | ✅ | Standard REST+HMAC, verified against MEXC's public docs (2026-09-04). Smoke-tested live via `check-auth` against the real account — works. |
| Aster | ✅ (REST auth fixed; withdraw-authorization struct unverified live) | ✅ (untested live) | Deposit is a documented vault `depositFor` call, not yet exercised live. `check-auth --dex Aster` passes (`$0.00`, real). Request-level auth is fixed (`aster_signing.py`, shared with `balances.py`). One real unknown remains: the SEPARATE EIP-712 "Action" struct that authorizes the withdrawal amount/destination itself (`_withdraw_action_signature`) is still a best-effort field reconstruction from Aster's docs, never exercised against a real withdraw. Fails closed if wrong (Aster rejects the signature, no funds move) — but **do not attempt a live Aster withdraw as a first test of this.** |
| Ondo Perps | ✅ (confirmed live) | ✅ (confirmed live) | The earlier "no documented withdraw, deposit needs a Bearer JWT" verdict was based on the "Builder Integration Guide" page alone; the separate `docs.ondoperps.xyz/api-reference/wallet/*` pages document both `POST /v1/withdraw` and `POST /v1/provision_address` against the same `ONDO_API_KEY`/`ONDO_API_SECRET` pair `balances.py::_ondo` already uses (ONDO-KEY-ID/ONDO-TIMESTAMP/ONDO-SIGN, not the docs' bare `X-API-KEY-ID` name). **Deposit confirmed live 2026-09-07**: a real $1.00 Arbitrum-USDC transfer to a freshly `provision_address`'d address, credited (`poll_balance_usd` $2.12→$3.12), tx `0x822f9ece4759e02655f9cd5e3a21fa524d13e0d491e5f980f185d544aa668b9e`. Arbitrum isn't in any api-reference page's documented `network` enum (only `ethereum`/`avalanche`/`solana` are) but works — this account's deposit addresses turn out to be shared across all three EVM networks Ondo watches, not per-network vaults. **Withdraw confirmed live 2026-09-07** ($1.50, `externalId f12fbd50...`) after fixing a real bug hit along the way: the first attempt 400'd `withdrawal_address_not_found` for this account's own wallet address even though it WAS in the address book — the book stores it lowercase, `wallet.address` is EIP-55 checksummed, and Ondo's lookup doesn't normalize case before comparing (`runners/ondo.py::_address_book_entry` now looks it up case-insensitively and sends back whatever string is actually on file). `executor.py`'s before/after on-chain balance poll (not just the REST "pending" response) confirmed the wallet's real balance rose by the full $1.50 — `actualCostUsd: 0.0`, i.e. `withdrawalFeeUSD`'s reported "$1" was NOT actually deducted this time, worth re-checking on a larger withdrawal. See `runners/ondo.py` docstring for the full trail. |
| Aden | ✅ **confirmed live** (2026-09-07) | ✅ **confirmed live** (2026-09-07) | **BSC only.** Reverse-engineered live 2026-09-07 by capturing the real web app's own network + wallet-signing traffic (see `runners/aden.py` docstring) — withdraw/login are on a completely separate host pair (`perps.aden.io` / `brokerapi.gateperps.com`) from the HMAC-keyed `api.aden.io` `balances.py::_aden` already uses. Login is fully headless (`_login()` does a `personal_sign` over Aden's challenge and mints its own `perp_evm_access_token`). Deposit: a plain ERC-20 transfer to Aden's own "Deposit" screen address — **confirmed live: a real deposit landed and was credited.** Withdraw: **confirmed live** — a real $10.50 BSC withdraw, `$10.30` received ($0.20 fee, matching config), real on-chain balance increase verified. Getting there took a real HAR capture of a successful browser withdrawal to find two independent bugs that had been masking each other behind a generic `P_FOMOX_IN_INTERNAL_ERROR` 500: (1) `x-perp-broker-main-uid` was sent as the wrong uid (`perp_evm_uid` instead of `perp_main_uid` — two separate fields this connector had conflated), and (2) the `Authorization: Bearer <general access_token>` header, wrongly removed in an earlier fix attempt that reasoned from the HAR alone (which doesn't show browser session cookies) — both had to be fixed together, since either one alone still 401'd. Nine other single-field guesses (casing, amount formatting, explicit nonce, extra headers) were tried and ruled out before the real capture settled it — see `runners/aden.py`'s docstring for the full trail. `_check_min_withdraw` (a separate, earlier-fixed bug: the real minimum is $10.20 BSC / $10.50 Arbitrum) remains in place. |
| Gate (Perp DEX) | ❌ | ❌ | Excluded for this v1 by explicit request. |
| Extended | ✅ (untested live — real reads/quote/signature confirmed, final submit not attempted, see notes) | ❌ | Checked directly against `api.docs.extended.exchange` and the real `x10-python-trading-starknet` PyPI package (2026-09-06) — Extended is StarkEx-derived, so every write (including withdraw) needs a STARK-curve signature, not the plain API key reads use. Withdraw is a 4-step Rhino.fi bridge flow to Arbitrum, documented on Extended's own side (`GET /user/bridge/config` → `GET /user/bridge/quote` → `POST /user/bridge/quote` commit → `POST /user/withdrawal`, STARK-signed); the signature itself uses `fast_stark_crypto`, the same Rust-backed library Extended's own SDK calls, run in a **dedicated Python 3.9 venv** (`runners/extended_signers/`) since it has no Python 3.14 wheel and refuses to build for one at all — confirmed by trying, not assumed. Verified live: the derived public key from `EXTENDED_STARK_PRIVATE_KEY` matches this account's own `l2Key` from a real `GET /user/account/info` call exactly, and a real `GET /user/bridge/quote` + a real signed settlement object were both produced successfully (fee: $0.01 on a $5 test quote). The final `POST /user/withdrawal` was deliberately not called — this account's real balance ($0.44) is below any sensible test amount. **Deposit not implemented**: its last step calls Rhino.fi's own `depositWithId` contract, whose ABI isn't documented on Extended's side. |
| dYdX | ❌ | ❌ | Checked directly against `docs.dydx.xyz` and the real `dydx-v4-client` PyPI package (2026-09-06) — a withdraw here is THREE hops, not one. Hop 1 (subaccount → your own dYdX main account) is solid: `NodeClient.withdraw()` really does sign+broadcast a `MsgWithdrawFromSubaccount`. Hop 2 (dYdX main account → Noble, IBC) has no working vendor implementation — the SDK's own `NobleClient.send_token_ibc()` is unfinished as published (computes `coin = token.coin()` and the function just ends, no message, no return). Hop 3 (Noble → Arbitrum, Circle's CCTP `MsgDepositForBurn`) isn't in the SDK at all. Hand-rolling hops 2/3 means an unverified CCTP burn call with no reference implementation — get the destination-domain/recipient-padding wrong and the USDC burns with nothing minting on the other end, irreversibly. The real alternative, Skip Go (dYdX's own documented bridge partner, real public API at `api.skip.build`, confirmed reachable), returns pre-built messages for the whole route instead — but needs a new Cosmos mnemonic credential and real protobuf tx signing against two chains, decided against building for now. **Balance reading works** (`balances.py`), no signing needed. |
| Lighter | ✅ (untested live) | ❌ | Withdraw only, by explicit instruction — a "fast withdraw" (L2 transfer to a pool account, destination address riding in the memo), signed via the same compiled Go binary `lighter-sdk` vendors internally (see `runners/lighter_signers/README.md`). Deposit and the "secure" on-chain withdrawal path were not built. |
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
