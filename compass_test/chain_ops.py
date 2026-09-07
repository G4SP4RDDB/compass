"""On-chain primitives shared by every DEX runner's deposit leg: build/sign/
send a transaction on BSC/Arbitrum and measure its REAL gas cost.

Reuses the exact same Alchemy endpoint/network map and USD price source as
src/graph — connectors.alchemy.AlchemyConnector.get_usd_price is what
costing.py itself uses to convert gas into USD, so the "actual" side of the
comparison isn't biased by a different price feed than the "estimated" side.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from web3 import Web3
from web3.contract import Contract

from connectors.alchemy import ALCHEMY_NETWORK_SLUG_BY_CHAIN, ALCHEMY_URL_TEMPLATE, AlchemyConnector
from connectors.chain_metadata import get_metadata
from connectors.config import require_alchemy_api_key
from connectors.exceptions import UnsupportedChainError
from connectors.stable_tokens import get_stable_decimals, get_stable_token_address
from graph.structures.DEXes import Chain, Stable

# Minimal ERC-20 ABI: only what a deposit ever needs (plain transfer, a
# balance read for polling a credit, and allowance/approve for a PULL-based
# deposit — a bridge contract that takes funds via transferFrom instead of
# receiving a plain transfer, e.g. Rhino.fi's, see runners/extended.py).
# Not the full standard — deliberately small so there's less surface to get
# wrong on a money-moving call.
ERC20_ABI = [
    {
        "constant": False,
        "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}, {"name": "_spender", "type": "address"}],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [{"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
]


def get_web3(chain: Chain) -> Web3:
    slug = ALCHEMY_NETWORK_SLUG_BY_CHAIN.get(chain)
    if slug is None:
        raise UnsupportedChainError("compass_test.chain_ops", chain)
    url = ALCHEMY_URL_TEMPLATE.format(network=slug, api_key=require_alchemy_api_key())
    return Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 20}))


def erc20_contract(w3: Web3, chain: Chain, stable: Stable) -> Contract:
    address = Web3.to_checksum_address(get_stable_token_address(chain, stable))
    return w3.eth.contract(address=address, abi=ERC20_ABI)


def usd_to_token_units(chain: Chain, stable: Stable, amount_usd: float) -> int:
    """Decimals are PER (chain, stable) — never a single global assumption.
    BSC's Binance-Peg USDT/USDC are 18 decimals on-chain, every other chain
    here is 6 (see connectors.stable_tokens.get_stable_decimals, and its
    docstring for how this was actually discovered: a BSC/USDT balance read
    with the old hardcoded 6 assumption showed ~$2,000,000,000,000 instead
    of ~$2 — a 10^12 error that would have made a real BSC deposit send
    dust instead of the intended amount)."""
    return round(amount_usd * 10 ** get_stable_decimals(chain, stable))


def token_units_to_usd(chain: Chain, stable: Stable, units: int) -> float:
    return units / 10 ** get_stable_decimals(chain, stable)


def get_stable_balance_usd(w3: Web3, chain: Chain, stable: Stable, address: str) -> float:
    contract = erc20_contract(w3, chain, stable)
    units = contract.functions.balanceOf(Web3.to_checksum_address(address)).call()
    return token_units_to_usd(chain, stable, units)


def get_stable_allowance(w3: Web3, chain: Chain, stable: Stable, owner: str, spender: str) -> int:
    """Current allowance in TOKEN UNITS (not USD) — a pull-based deposit
    (transferFrom, e.g. Rhino.fi's depositWithId) needs this checked before
    building the deposit call itself, or it would revert on-chain against an
    insufficient allowance rather than failing with a clear reason."""
    contract = erc20_contract(w3, chain, stable)
    return contract.functions.allowance(Web3.to_checksum_address(owner), Web3.to_checksum_address(spender)).call()


def build_erc20_approve_tx(w3: Web3, chain: Chain, stable: Stable, from_address: str, spender: str, amount_usd: float) -> dict:
    """Unsigned tx dict approving `spender` (e.g. a bridge contract) to pull
    up to `amount_usd` via transferFrom — the prerequisite step for any
    PULL-based deposit, see get_stable_allowance."""
    contract = erc20_contract(w3, chain, stable)
    call = contract.functions.approve(Web3.to_checksum_address(spender), usd_to_token_units(chain, stable, amount_usd))
    return _build_tx(w3, chain, from_address, call)


def build_erc20_transfer_tx(
    w3: Web3, chain: Chain, stable: Stable, from_address: str, to_address: str, amount_usd: float
) -> dict:
    """Unsigned tx dict for a plain ERC-20 transfer — the deposit shape used
    by any DEX that credits deposits by "send tokens to my deposit address"
    (e.g. MEXC), as opposed to a DEX-specific contract call (e.g. Aster's
    vault depositFor, see runners/aster.py)."""
    contract = erc20_contract(w3, chain, stable)
    call = contract.functions.transfer(Web3.to_checksum_address(to_address), usd_to_token_units(chain, stable, amount_usd))
    return _build_tx(w3, chain, from_address, call)


def build_contract_call_tx(w3: Web3, chain: Chain, from_address: str, call, value_wei: int = 0) -> dict:
    """Same as build_erc20_transfer_tx but for an arbitrary bound contract
    function call (e.g. a DEX's own deposit/vault contract) — the caller
    builds `call = contract.functions.someMethod(...)`, this fills in
    nonce/gas/chainId and estimates gas against current chain state.

    `value_wei` is native-currency msg.value, for a payable call that
    charges its fee that way instead of (or on top of) the ERC-20 amount —
    e.g. Aden's Orderly-derived Vault.deposit, which is payable and takes a
    LayerZero cross-chain messaging fee as msg.value (see runners/aden.py)."""
    return _build_tx(w3, chain, from_address, call, value_wei)


def _build_tx(w3: Web3, chain: Chain, from_address: str, call, value_wei: int = 0) -> dict:
    checksummed = Web3.to_checksum_address(from_address)
    nonce = w3.eth.get_transaction_count(checksummed, "pending")
    tx = call.build_transaction(
        {
            "from": checksummed,
            "nonce": nonce,
            "chainId": get_metadata(chain).chain_id,
            "value": value_wei,
            **_fee_params(w3),
        }
    )
    # +20% safety margin over the simulated estimate: a same-block balance
    # change between estimate and send (e.g. the withdraw we just polled for)
    # can otherwise make the exact estimate insufficient.
    tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.2)
    return tx


# How far above the base fee this pads maxFeePerGas. Not sized for
# congestion — it exists purely to absorb the base fee moving between when
# we read it here and when the node re-checks it a call later (estimate_gas,
# then later signing/sending): observed drift between two RPC round trips a
# moment apart on Arbitrum has been ~0.3-0.5%, so this is a large multiple of
# the actual drift, not a tight fit to it (see the "max fee per gas less
# than block base fee" failure this replaces).
_BASE_FEE_MARGIN = 1.2


def _fee_params(w3: Web3) -> dict:
    """EIP-1559 fee fields on a chain that actually runs a base-fee market
    (Arbitrum); a plain legacy gasPrice otherwise (BSC's fee_history reports
    baseFeePerGas=0 — no dynamic base fee to race against, so padding one
    would be meaningless).

    Reads the base fee via fee_history rather than get_block("latest"): a
    block header on BSC (proof-of-authority, Clique extraData) trips web3.py's
    default ExtraDataLengthError validation, while fee_history returns just
    the fee series, no header. Its baseFeePerGas array's LAST entry is
    already the chain's own projection for the NEXT block — the block this
    tx will actually try to land in, one step better than reading the
    current block's fee.
    """
    base_fee = w3.eth.fee_history(1, "latest", [])["baseFeePerGas"][-1]
    if not base_fee:
        return {"gasPrice": w3.eth.gas_price}
    priority_fee = w3.eth.max_priority_fee
    return {
        "maxFeePerGas": int(base_fee * _BASE_FEE_MARGIN) + priority_fee,
        "maxPriorityFeePerGas": priority_fee,
    }


@dataclass
class OnChainTxResult:
    tx_hash: str
    submitted_at: float
    confirmed_at: float
    gas_used: int
    effective_gas_price_wei: int
    gas_cost_usd: float


def _raw_bytes(signed_tx) -> bytes:
    # eth_account has renamed this attribute across major versions
    # (rawTransaction -> raw_transaction) — accept either rather than pin an
    # exact web3/eth-account version here.
    return getattr(signed_tx, "raw_transaction", None) or signed_tx.rawTransaction


def send_and_wait(
    w3: Web3, chain: Chain, signed_tx, alchemy: AlchemyConnector | None = None, timeout_s: int = 180
) -> OnChainTxResult:
    alchemy = alchemy or AlchemyConnector()
    submitted_at = time.time()
    tx_hash = w3.eth.send_raw_transaction(_raw_bytes(signed_tx))
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout_s)
    confirmed_at = time.time()
    if receipt.status != 1:
        raise RuntimeError(f"on-chain tx {tx_hash.hex()} on {chain.name} reverted (receipt status 0)")

    effective_gas_price = receipt.get("effectiveGasPrice") or w3.eth.gas_price
    native_amount = receipt["gasUsed"] * effective_gas_price / 1e18
    gas_cost_usd = native_amount * alchemy.get_usd_price(chain)

    return OnChainTxResult(
        tx_hash=tx_hash.hex(),
        submitted_at=submitted_at,
        confirmed_at=confirmed_at,
        gas_used=receipt["gasUsed"],
        effective_gas_price_wei=effective_gas_price,
        gas_cost_usd=gas_cost_usd,
    )


def estimate_dry_run_gas_cost_usd(w3: Web3, chain: Chain, tx: dict, alchemy: AlchemyConnector | None = None) -> float:
    """Cost estimate for a dry-run: the tx was already simulated against live
    state by _build_tx (eth_estimateGas), so this just prices tx['gas'] at
    the current gas price/USD rate — no signing, no broadcast.

    tx['maxFeePerGas'], when present, is the padded CEILING _fee_params set
    (see _BASE_FEE_MARGIN) — a live send typically pays somewhat less than
    this, since EIP-1559 only ever charges min(maxFeePerGas, baseFee +
    priorityFee) and refunds the rest. So this estimate is a deliberately
    conservative upper bound on that chain, not the expected price, same as
    it already was on a legacy-gasPrice chain wherever gas price itself
    happened to move before the real send."""
    alchemy = alchemy or AlchemyConnector()
    price_per_gas = tx.get("maxFeePerGas", tx.get("gasPrice"))
    native_amount = tx["gas"] * price_per_gas / 1e18
    return native_amount * alchemy.get_usd_price(chain)
