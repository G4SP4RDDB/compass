"""The Solana counterpart of wallet.OperatingWallet: the address that
receives CCTP-minted USDC on Solana (the destination of the Arbitrum->Solana
withdraw pipeline, see connectors/cctp.py) and signs the receiveMessage tx
that actually triggers that mint.

Separate key material from the EVM operating wallet (ed25519, not
secp256k1) — never assume the two addresses are related. Same laziness
contract as OperatingWallet: the private key is only read from the
environment (via config.SOLANA_WALLET_KEY_VAR) the first time a transaction
actually needs SIGNING, never just to read `.address` — a dry run (balance
polling) only needs the public address, so it can run from
COMPASS_TEST_SOLANA_WALLET_ADDRESS alone, with no private key material on
the machine at all.
"""

from __future__ import annotations

from solders.keypair import Keypair
from solders.pubkey import Pubkey

from . import config


class SolanaWallet:
    def __init__(self, private_key_env_var: str | None = None, known_address: str | None = None):
        self._keypair: Keypair | None = None
        self._private_key_env_var = private_key_env_var
        self._known_address = Pubkey.from_string(known_address) if known_address else None

    def _resolve_env_var(self) -> str:
        var = self._private_key_env_var or config.SOLANA_WALLET_KEY_VAR
        if not var:
            raise RuntimeError(
                "No Solana wallet key configured. Set COMPASS_TEST_SOLANA_WALLET_KEY_VAR in "
                "compass/.env to the NAME of the env var holding the wallet's base58-encoded "
                "secret key (e.g. COMPASS_TEST_SOLANA_WALLET_KEY_VAR=COMPASS_TEST_SOLANA_WALLET_PRIVATE_KEY) "
                "— see .env.example."
            )
        return var

    @property
    def keypair(self) -> Keypair:
        if self._keypair is None:
            var = self._resolve_env_var()
            raw_key = config.require_env(var)
            # solana-keygen's own base58 secret-key string (64 bytes: 32-byte
            # seed + 32-byte pubkey) — the same format `solders.Keypair` uses
            # for its own __str__/to_base58_string, and what a private key
            # copied out of a wallet UI (Phantom, etc.) normally looks like.
            self._keypair = Keypair.from_base58_string(raw_key)
        return self._keypair

    @property
    def address(self) -> str:
        # A dry run only ever reads this — resolve from a known public
        # address first (COMPASS_TEST_SOLANA_WALLET_ADDRESS) so it works
        # with zero private key material present; a live run always signs
        # via `keypair`, which re-derives the address from the real key
        # anyway, so there's no risk of the two silently diverging on a
        # live call (same contract as wallet.OperatingWallet.address).
        if self._known_address is not None:
            return str(self._known_address)
        return str(self.keypair.pubkey())

    @property
    def pubkey(self) -> Pubkey:
        if self._known_address is not None and self._keypair is None:
            return self._known_address
        return self.keypair.pubkey()
