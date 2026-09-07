# Extended STARK signer (dedicated Python 3.9 venv)

Extended's write operations (withdrawals, transfers, orders) are authorized
by a STARK-curve signature — a different curve from everything else in this
repo (Aster/MEXC/Hyperliquid/Lighter's L1 leg all sign with secp256k1). The
official crypto primitive is `fast_stark_crypto`
(https://pypi.org/project/fast-stark-crypto/), the same Rust-backed library
Extended's own `x10-python-trading-starknet` SDK uses internally
(`x10/signing/withdrawal_object.py`, `x10/core/stark_account.py` —
https://github.com/x10xchange/python_sdk/tree/starknet).

**Why a separate venv**: `fast_stark_crypto` ships prebuilt wheels for
CPython 3.9 through 3.13 (all platforms, including macOS arm64) but NOT for
3.14, which is what this project's own `.venv` runs. Installing it there
falls back to building from source, and its pinned PyO3 version (0.22.6)
refuses outright — "the configured Python interpreter version (3.14) is
newer than PyO3's maximum supported version (3.13)" — confirmed by actually
trying it (2026-09-06), not assumed. Forcing the build past that check
(`PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1`) is an explicitly "at your own risk"
escape hatch upstream, not something to gamble on for a fund-moving
signature. A real prebuilt wheel under a real supported interpreter is
worth a second venv instead.

`.venv39` here was created from the system's Python 3.9.6 (`python -m venv`;
gitignored, like `.venv`/`.venv-1` — recreate it locally with:

```bash
python3.9 -m venv compass_test/runners/extended_signers/.venv39
compass_test/runners/extended_signers/.venv39/bin/pip install fast_stark_crypto
```

`runners/extended.py` never imports `fast_stark_crypto` directly — it shells
out to `sign_withdrawal.py` in THIS venv via `subprocess`, passing the
withdrawal's signing inputs as JSON on stdin and reading `{"r", "s"}` back on
stdout. The private key crosses that boundary on stdin only, never as a
command-line argument (so it never lands in `ps` output) and is never
logged.

Verified 2026-09-06: `fast_stark_crypto.get_public_key(EXTENDED_STARK_PRIVATE_KEY)`
computed here matches this account's own `l2Key` from a live
`GET /user/account/info` call exactly — real key material, not a guess.
