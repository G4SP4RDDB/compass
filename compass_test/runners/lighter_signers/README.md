# Lighter native signer

`lighter-signer-darwin-arm64.dylib` (+ its `.h` header) is Lighter's own
compiled Go signer for its L2 (StarkEx-style) transaction format — the same
binary `piggybank-arb/src/dex/lighter/signers/` vendors and loads via Node
FFI (`koffi`, see that project's `signer.ts`), and the same one the
`lighter-sdk` PyPI package vendors internally and loads via `ctypes`
(`lighter/signer_client.py` in that package).

Copied here directly (2026-09-06) rather than depending on `lighter-sdk` as
a pip package: that package's own metadata requires `eth-account>=0.13.4`,
which conflicts with `web3==6.20.4`'s `eth-account<0.13` — installing it
broke every other connector in this repo. `runners/lighter.py` loads this
`.dylib` straight via Python's built-in `ctypes` (no extra dependency),
mirroring `lighter-sdk`'s own `signer_client.py` call sequence exactly
(`CreateClient` -> `CheckClient` -> `CreateAuthToken` / `SignTransfer`) —
see that connector's docstring.

Only the macOS ARM64 binary is vendored, since that's what this project
actually runs on. Add the matching binary from
`piggybank-arb/src/dex/lighter/signers/` (or lighter-python's own package)
for another platform if ever needed — same filenames, same C ABI.

Built from the Go implementation in https://github.com/elliottech/lighter-go.
