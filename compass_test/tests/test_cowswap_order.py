"""CoW order building/signing — pure math, no network, no real key.

The EIP-712 digest check is the one that matters: it recomputes the order
struct hash BY HAND from GPv2Order.sol's TYPE_HASH and abi.encode, exactly
as the settlement contract does on-chain, and asserts eth_account's
encode_typed_data over connectors.cowswap.order_typed_data lands on the
same bytes. A mismatch there (a field renamed, reordered, or typed
differently) would make the orderbook recover a different signer and reject
every order — this test fails first."""

from __future__ import annotations

import time

import pytest
from eth_abi import encode
from eth_account import Account
from eth_account.messages import _hash_eip191_message, encode_typed_data
from eth_utils import keccak

from connectors import cowswap
from graph.structures.DEXes import Chain

_SELL = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"  # Arbitrum USDC
_BUY = "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9"  # Arbitrum USDT
_RECEIVER = "0x477899D0e04E8b510ADA7EcD1Db22d1BdF3F1650"


def _quote(sell=986_760, fee=13_240, buy=986_882, valid_to=None) -> cowswap.CowQuote:
    return cowswap.CowQuote(
        chain=Chain.ARBITRUM,
        sell_token=_SELL,
        buy_token=_BUY,
        receiver=_RECEIVER,
        sell_amount=sell,
        fee_amount=fee,
        buy_amount=buy,
        valid_to=valid_to or int(time.time()) + 600,
        quote_id=131555317,
        verified=True,
        expiration="",
    )


class TestConstants:
    def test_order_type_hash_matches_gpv2order_sol(self):
        type_string = "Order(" + ",".join(f"{f['type']} {f['name']}" for f in cowswap.ORDER_TYPE_FIELDS) + ")"
        assert "0x" + keccak(text=type_string).hex() == cowswap.ORDER_TYPE_HASH

    def test_empty_app_data_hash_is_keccak_of_the_literal(self):
        assert "0x" + keccak(text=cowswap.EMPTY_APP_DATA).hex() == cowswap.EMPTY_APP_DATA_HASH

    def test_both_target_chains_have_an_orderbook_and_a_chain_id(self):
        for chain in (Chain.BSC, Chain.ARBITRUM):
            assert cowswap.CowSwapConnector.supports(chain)
            assert cowswap.eip712_domain(chain)["chainId"] in (56, 42161)


class TestBuildOrder:
    def test_fee_folded_into_sell_amount_and_fee_field_zero(self):
        order = cowswap.build_order_from_quote(_quote(), slippage_bps=0)

        assert order.sell_amount == 986_760 + 13_240
        assert order.fee_amount == 0
        assert order.buy_amount == 986_882
        assert order.kind == "sell" and order.partially_fillable is False
        assert order.receiver == _RECEIVER

    def test_slippage_lowers_min_buy_amount_only(self):
        order = cowswap.build_order_from_quote(_quote(), slippage_bps=50)

        assert order.buy_amount == 986_882 * 9_950 // 10_000
        assert order.sell_amount == 1_000_000

    def test_explicit_receiver_overrides_quote_receiver(self):
        order = cowswap.build_order_from_quote(_quote(), slippage_bps=0, receiver="0x" + "11" * 20)
        assert order.receiver == "0x" + "11" * 20

    def test_out_of_range_slippage_is_refused(self):
        with pytest.raises(ValueError):
            cowswap.build_order_from_quote(_quote(), slippage_bps=10_001)


class TestEip712Digest:
    def test_library_digest_equals_hand_rolled_gpv2_digest(self):
        order = cowswap.build_order_from_quote(_quote(valid_to=1_800_000_000), slippage_bps=50)
        typed = cowswap.order_typed_data(Chain.ARBITRUM, order)
        library_digest = _hash_eip191_message(encode_typed_data(full_message=typed))

        struct_hash = keccak(
            encode(
                ["bytes32", "address", "address", "address", "uint256", "uint256", "uint32", "bytes32", "uint256", "bytes32", "bool", "bytes32", "bytes32"],
                [
                    bytes.fromhex(cowswap.ORDER_TYPE_HASH[2:]),
                    order.sell_token,
                    order.buy_token,
                    order.receiver,
                    order.sell_amount,
                    order.buy_amount,
                    order.valid_to,
                    bytes.fromhex(order.app_data_hash[2:]),
                    order.fee_amount,
                    keccak(text="sell"),
                    False,
                    keccak(text="erc20"),
                    keccak(text="erc20"),
                ],
            )
        )
        domain_separator = keccak(
            encode(
                ["bytes32", "bytes32", "bytes32", "uint256", "address"],
                [
                    keccak(text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
                    keccak(text="Gnosis Protocol"),
                    keccak(text="v2"),
                    42161,
                    cowswap.SETTLEMENT_CONTRACT,
                ],
            )
        )
        assert library_digest == keccak(b"\x19\x01" + domain_separator + struct_hash)

    def test_domain_differs_per_chain(self):
        order = cowswap.build_order_from_quote(_quote(valid_to=1_800_000_000), slippage_bps=0)
        bsc = _hash_eip191_message(encode_typed_data(full_message=cowswap.order_typed_data(Chain.BSC, order)))
        arb = _hash_eip191_message(encode_typed_data(full_message=cowswap.order_typed_data(Chain.ARBITRUM, order)))
        assert bsc != arb

    def test_signature_recovers_the_signing_account(self):
        account = Account.create()
        order = cowswap.build_order_from_quote(_quote(valid_to=1_800_000_000), slippage_bps=0)
        signable = encode_typed_data(full_message=cowswap.order_typed_data(Chain.BSC, order))
        signed = account.sign_message(signable)
        assert Account.recover_message(signable, signature=signed.signature) == account.address

    def test_cancellation_typed_data_encodes(self):
        uid = "0x" + "ab" * 56
        signable = encode_typed_data(full_message=cowswap.cancellations_typed_data(Chain.BSC, [uid]))
        assert signable is not None


class TestCreationPayload:
    def test_payload_carries_from_app_data_and_string_amounts(self):
        order = cowswap.build_order_from_quote(_quote(), slippage_bps=50)
        payload = cowswap.order_creation_payload(order, "0xsig", _RECEIVER, quote_id=7)

        assert payload["from"] == _RECEIVER
        assert payload["appData"] == "{}" and payload["appDataHash"] == cowswap.EMPTY_APP_DATA_HASH
        assert payload["sellAmount"] == "1000000" and payload["feeAmount"] == "0"
        assert payload["signingScheme"] == "eip712" and payload["signature"] == "0xsig"
        assert payload["quoteId"] == 7
