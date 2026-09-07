"""Blanket sanity check over every hardcoded CEX deposit address (see
config.expected_deposit_addresses) — catches the two ways one of these gets
entered wrong without anyone noticing until a live run tries to use it:
a copy-paste that lands on the zero/placeholder address, or a value that
isn't a syntactically valid EVM address at all (typo, missing "0x", wrong
length). Neither failure mode is specific to any one DEX/chain, so this
tests the whole config rather than one entry at a time."""

from __future__ import annotations

from web3 import Web3

from compass_test import config

_ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


class TestExpectedDepositAddresses:
    def test_at_least_one_pinned_address_is_configured(self):
        # Not "must have every DEX/chain pinned" (pinning is opt-in per
        # (dex, chain), see config.py) — just a guard against this whole
        # mechanism silently doing nothing because the .env entry it's meant
        # to read got renamed/removed.
        assert config.expected_deposit_addresses(), (
            "no COMPASS_TEST_EXPECTED_DEPOSIT_ADDRESS_* env var is set — "
            "expected at least the MEXC/BSC one from .env"
        )

    def test_no_pinned_address_is_the_zero_address(self):
        zeroed = [
            name
            for name, address in config.expected_deposit_addresses().items()
            if address.lower() == _ZERO_ADDRESS
        ]
        assert not zeroed, f"pinned to the zero address (likely an unset/placeholder value): {zeroed}"

    def test_every_pinned_address_is_a_syntactically_valid_evm_address(self):
        invalid = [
            f"{name}={address!r}"
            for name, address in config.expected_deposit_addresses().items()
            if not Web3.is_address(address)
        ]
        assert not invalid, f"not a valid EVM address (typo? missing 0x? wrong length?): {invalid}"
