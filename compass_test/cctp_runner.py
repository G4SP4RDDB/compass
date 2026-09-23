"""Execution glue for the Arbitrum -> Solana CCTP withdraw pipeline (see
connectors/cctp.py for the protocol layer this wraps — PDA/calldata/
attestation primitives, all wallet-agnostic). This module is where that
protocol layer actually meets OperatingWallet/SolanaWallet and gets signed
and sent, same split as every other DEX runner in compass_test/runners/
(protocol details in connectors/, signing/sending here).

Wired into executor.py's Bridge dispatch (see executor._run_cctp_bridge) —
a Bridge hop between ARBITRUM and SOLANA routes here instead of through
Aden's ledger, chosen by graph.structures.bridges.availableBridgeProtocols
(same protocol dispatch plan_loader.py's cost/time estimate already uses).

Safety: this module does NOT itself check config.ALLOW_LIVE or the $ caps —
same contract as _run_bridge/_run_withdraw etc. in executor.py, which check
those BEFORE calling down into a runner. Calling execute_arbitrum_to_
solana_withdrawal directly (bypassing executor.run_hop) skips that gate
entirely and `live=True` WILL sign and broadcast real transactions with no
cap/confirmation of its own — treat it with the same care as executor.
run_hop's own live path, prefer going through run_hop/run_single_hop.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Callable

import requests
from solders.pubkey import Pubkey
from solders.transaction import Transaction

from connectors import cctp
from connectors.config import SOLANA_RPC_URL
from connectors.exceptions import ConnectorAPIError
from connectors.stable_tokens import get_stable_token_address
from graph.structures.DEXes import Chain, Stable

from . import chain_ops
from .solana_wallet import SolanaWallet
from .wallet import OperatingWallet

_NOOP_STAGE: Callable[[str, str, str], None] = lambda *_args: None


@dataclass
class CctpWithdrawalResult:
    live: bool
    amountUsd: float
    burnTxHash: str | None = None
    burnGasCostUsd: float | None = None
    messageHex: str | None = None
    attestationHex: str | None = None
    receiveMessageSignature: str | None = None
    receiveMessageGasCostUsd: float | None = None
    notes: str = ""

    @property
    def totalCostUsd(self) -> float | None:
        """CCTP V1 has no protocol fee (bridgeFeeUsd returns 0.0 for it, see
        graph.structures.bridges) — the whole cost is gas on both legs."""
        if self.burnGasCostUsd is None:
            return None
        if self.receiveMessageGasCostUsd is None:
            return self.burnGasCostUsd
        return self.burnGasCostUsd + self.receiveMessageGasCostUsd


def _ensure_allowance(w3, wallet: OperatingWallet, amount_usd: float, spender: str, live: bool, on_stage) -> None:
    """depositForBurn pulls funds via transferFrom (see connectors/cctp.py's
    module docstring reference to CCTP's burn mechanics) — same PULL shape
    as Rhino.fi's deposit (see chain_ops.get_stable_allowance's own
    docstring), so it needs an approve() first if the current allowance is
    short. Approves the EXACT amount needed, not infinite — this wallet is
    test-only funds per its own docstring, but there's no reason to hand
    TokenMessenger a standing infinite allowance for that."""
    current_units = chain_ops.get_stable_allowance(w3, Chain.ARBITRUM, Stable.USDC, wallet.address, spender)
    current_usd = chain_ops.token_units_to_usd(Chain.ARBITRUM, Stable.USDC, current_units)
    if current_usd >= amount_usd:
        return
    on_stage("approve", "building", f"allowance ${current_usd:.2f} < ${amount_usd:.2f}, approving")
    if not live:
        on_stage("approve", "dry_run", "would approve here")
        return
    tx = chain_ops.build_erc20_approve_tx(w3, Chain.ARBITRUM, Stable.USDC, wallet.address, spender, amount_usd)
    signed = wallet.sign_transaction(tx)
    result = chain_ops.send_and_wait(w3, Chain.ARBITRUM, signed)
    on_stage("approve", "confirmed", result.tx_hash)


def burn_on_arbitrum(
    amount_usd: float,
    solana_token_account: str,
    wallet: OperatingWallet,
    live: bool = False,
    on_stage: Callable[[str, str, str], None] | None = None,
) -> tuple[str | None, float | None, bytes | None]:
    """Approves (if needed) then calls TokenMessenger.depositForBurn on
    Arbitrum, destined for `solana_token_account` (a SPL token account
    address, NOT a wallet owner — see connectors.cctp.
    encode_mint_recipient_for_solana). Returns (txHash, gasCostUsd,
    messageBytes); messageBytes is None in a dry run (nothing was actually
    broadcast, so there's no receipt to scrape MessageSent from)."""
    on_stage = on_stage or _NOOP_STAGE
    w3 = chain_ops.get_web3(Chain.ARBITRUM)
    token_messenger = cctp.ARBITRUM_TOKEN_MESSENGER_V1_ADDRESS
    usdc_address = get_stable_token_address(Chain.ARBITRUM, Stable.USDC)

    _ensure_allowance(w3, wallet, amount_usd, token_messenger, live, on_stage)

    mint_recipient = cctp.encode_mint_recipient_for_solana(solana_token_account)
    amount_units = chain_ops.usd_to_token_units(Chain.ARBITRUM, Stable.USDC, amount_usd)
    contract = w3.eth.contract(address=w3.to_checksum_address(token_messenger), abi=cctp.TOKEN_MESSENGER_ABI)
    call = contract.functions.depositForBurn(
        amount_units,
        cctp.CCTP_DOMAIN_BY_CHAIN[Chain.SOLANA],
        mint_recipient,
        w3.to_checksum_address(usdc_address),
    )
    tx = chain_ops.build_contract_call_tx(w3, Chain.ARBITRUM, wallet.address, call)

    if not live:
        gas_cost = chain_ops.estimate_dry_run_gas_cost_usd(w3, Chain.ARBITRUM, tx)
        on_stage("burn", "dry_run", f"simulated gas ${gas_cost:.4f}")
        return None, gas_cost, None

    on_stage("burn", "signing", "")
    signed = wallet.sign_transaction(tx)
    result = chain_ops.send_and_wait(w3, Chain.ARBITRUM, signed)
    receipt = w3.eth.get_transaction_receipt(result.tx_hash)
    message = cctp.extract_message_from_receipt(receipt)
    on_stage("burn", "confirmed", result.tx_hash)
    return result.tx_hash, result.gas_cost_usd, message


def _get_latest_blockhash(rpc_url: str) -> str:
    response = requests.post(
        rpc_url,
        json={"jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash", "params": [{"commitment": "confirmed"}]},
        timeout=15,
    )
    if not response.ok:
        raise ConnectorAPIError("cctp_runner.solana", rpc_url, response.text)
    return response.json()["result"]["value"]["blockhash"]


def _send_raw_transaction(rpc_url: str, tx: Transaction) -> str:
    raw_b64 = base64.b64encode(bytes(tx)).decode()
    response = requests.post(
        rpc_url,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [raw_b64, {"encoding": "base64", "preflightCommitment": "confirmed"}],
        },
        timeout=30,
    )
    data = response.json()
    if "error" in data:
        raise ConnectorAPIError("cctp_runner.solana", rpc_url, str(data["error"]))
    return data["result"]


def _wait_for_confirmation(rpc_url: str, signature: str, timeout_s: float = 90.0, interval_s: float = 2.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        response = requests.post(
            rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": "getSignatureStatuses", "params": [[signature]]},
            timeout=15,
        )
        status = response.json()["result"]["value"][0]
        if status is not None:
            if status.get("err"):
                raise RuntimeError(f"Solana tx {signature} failed on-chain: {status['err']}")
            if status.get("confirmationStatus") in ("confirmed", "finalized"):
                return
        time.sleep(interval_s)
    raise TimeoutError(f"Solana tx {signature} not confirmed after {timeout_s}s")


def _get_transaction_fee_usd(rpc_url: str, signature: str) -> float | None:
    """Real lamports paid for the confirmed receiveMessage tx (meta.fee),
    converted to USD the same way connectors.solana_rpc's gas estimate
    does (Alchemy's Prices API — see connectors.gas.GasFeeService, the
    same feed costing.py itself uses, so this stays comparable to the
    solver's own estimate). None (not 0.0) if the price feed is down —
    same "don't report a fabricated number" convention as ExecutedHop's
    other cost fields."""
    from connectors.alchemy import AlchemyConnector
    from connectors.exceptions import ConnectorAPIError as _CAE
    from connectors.solana_rpc import LAMPORTS_PER_SOL
    from graph.structures.DEXes import Chain as _Chain

    response = requests.post(
        rpc_url,
        json={
            "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
            "params": [signature, {"encoding": "json", "maxSupportedTransactionVersion": 0}],
        },
        timeout=15,
    )
    result = response.json().get("result")
    if not result:
        return None
    fee_lamports = result["meta"]["fee"]
    try:
        usd_price = AlchemyConnector().get_usd_price(_Chain.SOLANA)
    except _CAE:
        return None
    return (fee_lamports / LAMPORTS_PER_SOL) * usd_price


def submit_receive_message_on_solana(
    message: bytes,
    attestation: bytes,
    user_token_account: str,
    wallet: SolanaWallet,
    live: bool = False,
    rpc_url: str = SOLANA_RPC_URL,
    on_stage: Callable[[str, str, str], None] | None = None,
) -> tuple[str | None, float | None]:
    """Submits receiveMessage on Solana, minting the bridged USDC into
    `user_token_account`. Returns (signature, gasCostUsd) — both None in a
    dry run (this still derives every PDA and builds the real instruction —
    the one thing it skips is actually sending it, see this module's own
    docstring on why a full simulateTransaction dry run isn't attempted
    here: the payer needs enough SOL for rent/fees for even a simulation to
    reflect reality, same caveat as any other live-state-dependent dry run
    in this codebase)."""
    on_stage = on_stage or _NOOP_STAGE
    decoded = cctp.decode_cctp_message(message)
    pdas = cctp.get_receive_message_pdas(
        source_domain=decoded.source_domain,
        remote_token_evm_address=get_stable_token_address(Chain.ARBITRUM, Stable.USDC),
        nonce=decoded.nonce,
    )
    instruction = cctp.build_receive_message_instruction(
        payer=wallet.pubkey,
        message=message,
        attestation=attestation,
        user_token_account=Pubkey.from_string(user_token_account),
        pdas=pdas,
    )
    on_stage("receive", "built", f"nonce={decoded.nonce} source_domain={decoded.source_domain}")

    if not live:
        on_stage("receive", "dry_run", "instruction built, not sent")
        return None, None

    blockhash = _get_latest_blockhash(rpc_url)
    tx = Transaction.new_signed_with_payer(
        [instruction], wallet.pubkey, [wallet.keypair], blockhash
    )
    signature = _send_raw_transaction(rpc_url, tx)
    on_stage("receive", "sent", signature)
    _wait_for_confirmation(rpc_url, signature)
    gas_cost_usd = _get_transaction_fee_usd(rpc_url, signature)
    on_stage("receive", "confirmed", signature)
    return signature, gas_cost_usd


def execute_arbitrum_to_solana_withdrawal(
    amount_usd: float,
    solana_token_account: str | None = None,
    evm_wallet: OperatingWallet | None = None,
    solana_wallet: SolanaWallet | None = None,
    relayer_wallet: SolanaWallet | None = None,
    live: bool = False,
    on_stage: Callable[[str, str, str], None] | None = None,
) -> CctpWithdrawalResult:
    """The whole pipeline: burn USDC on Arbitrum, wait for Circle's
    attestation (~13-19 min live, see connectors.cctp.poll_attestation),
    then mint it on Solana into `solana_token_account` — defaults to
    `solana_wallet`'s OWN USDC Associated Token Account (see connectors.
    cctp.get_usdc_associated_token_account) when not given, since this
    project's CCTP route only ever pays out into our own wallet (see
    graph.node.WalletNode/WalletDeficitNode — never a third-party
    recipient). That account must already exist (see connectors.cctp.
    encode_mint_recipient_for_solana) — this never creates it.

    `relayer_wallet` — who actually SIGNS and pays the Solana tx fee for
    the receive_message leg — defaults to `solana_wallet` (the old,
    only-ever-one-wallet behavior), but can be ANY funded Solana wallet:
    the CCTP program itself doesn't require the payer/caller to have any
    relationship to `user_token_account` (see connectors.cctp.
    build_receive_message_instruction — `payer`/`caller` and
    `user_token_account` are independent accounts in the instruction, and
    receive_message.rs's own destination_caller check only applies if the
    ORIGINAL depositForBurn set one, which burn_on_arbitrum never does).
    This is what makes a gas-sponsored/relayed receive possible: the wallet
    that ends up HOLDING the minted USDC never needs to hold any SOL
    itself — some other already-funded wallet (yours or a relay service's)
    pays that leg's tiny fee instead. Still needs at least one SOL-funded
    keypair somewhere, just not necessarily this one.

    A dry run goes through the burn's gas estimate and the receive
    instruction's PDA/account assembly, but never actually burns anything,
    so it also never has a real attestation to wait for or mint to submit
    — it stops after the burn leg."""
    from . import config

    on_stage = on_stage or _NOOP_STAGE
    evm_wallet = evm_wallet or OperatingWallet(known_address=config.OPERATING_WALLET_ADDRESS)
    solana_wallet = solana_wallet or SolanaWallet(known_address=config.SOLANA_WALLET_ADDRESS)
    relayer_wallet = relayer_wallet or solana_wallet
    if solana_token_account is None:
        solana_token_account = str(cctp.get_usdc_associated_token_account(solana_wallet.address))

    burn_tx_hash, burn_cost, message = burn_on_arbitrum(amount_usd, solana_token_account, evm_wallet, live, on_stage)

    if not live or message is None:
        return CctpWithdrawalResult(
            live=live,
            amountUsd=amount_usd,
            burnTxHash=burn_tx_hash,
            burnGasCostUsd=burn_cost,
            notes="dry run — burn leg simulated only, no attestation/receive attempted",
        )

    on_stage("attestation", "waiting", "Circle attestation, ~13-19 min on Arbitrum V1")
    attestation = cctp.poll_attestation(message, mainnet=True)
    on_stage("attestation", "ready", cctp.message_hash(message))

    signature, receive_gas_cost = submit_receive_message_on_solana(
        message, attestation, solana_token_account, relayer_wallet, live, on_stage=on_stage
    )

    return CctpWithdrawalResult(
        live=live,
        amountUsd=amount_usd,
        burnTxHash=burn_tx_hash,
        burnGasCostUsd=burn_cost,
        messageHex=cctp.message_hash(message),
        attestationHex="0x" + attestation.hex(),
        receiveMessageSignature=signature,
        receiveMessageGasCostUsd=receive_gas_cost,
        notes="complete",
    )
