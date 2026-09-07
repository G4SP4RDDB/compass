"""The operating wallet: the single EVM address that receives DEX withdrawals
and signs on-chain deposits (plain ERC-20 transfer or a DEX's own deposit
contract call), on BSC/Arbitrum.

Deliberately lazy: the private key is only read from the environment (via
config.OPERATING_WALLET_KEY_VAR, itself pointing at the real var name — see
config.py) the first time a transaction actually needs SIGNING, never just to
read `.address` — a dry run (gas estimation, balance polling) only needs the
public address, so it can run from COMPASS_TEST_WALLET_ADDRESS alone, with no
private key material on the machine at all.
"""

from __future__ import annotations

from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3 import Web3

from . import config


class OperatingWallet:
    def __init__(self, private_key_env_var: str | None = None, known_address: str | None = None):
        self._account: LocalAccount | None = None
        self._private_key_env_var = private_key_env_var
        self._known_address = Web3.to_checksum_address(known_address) if known_address else None

    def _resolve_env_var(self) -> str:
        var = self._private_key_env_var or config.OPERATING_WALLET_KEY_VAR
        if not var:
            raise RuntimeError(
                "No operating wallet key configured. Set COMPASS_TEST_WALLET_KEY_VAR in "
                "compass/.env to the NAME of the env var holding the wallet's private key "
                "(e.g. COMPASS_TEST_WALLET_KEY_VAR=ASTER_PRIVATE_KEY) — see README.md "
                "'Operating wallet'."
            )
        return var

    @property
    def account(self) -> LocalAccount:
        if self._account is None:
            var = self._resolve_env_var()
            raw_key = config.require_env(var)
            self._account = Account.from_key(raw_key)
        return self._account

    @property
    def address(self) -> str:
        # A dry run only ever reads this — resolve from a known public
        # address first (COMPASS_TEST_WALLET_ADDRESS) so it works with zero
        # private key material present; a live run always signs via
        # `account`, which re-derives the address from the real key anyway,
        # so there's no risk of the two silently diverging on a live call.
        if self._known_address is not None:
            return self._known_address
        return self.account.address

    def sign_transaction(self, tx: dict):
        return self.account.sign_transaction(tx)

    def sign_typed_data(self, full_message: dict):
        """EIP-712 signature over `full_message` ({domain, types, primaryType,
        message}), used by runners that need a wallet-signed withdraw request
        (e.g. Aster) in addition to their REST API-key auth."""
        from eth_account.messages import encode_typed_data

        signable = encode_typed_data(full_message=full_message)
        return self.account.sign_message(signable)
