"""Circle's Cross-Chain Transfer Protocol (CCTP), V1 (Standard Transfer)
only — the only protocol/version graph.structures.bridges models today (see
its own comment: WalletDeficitNode has no urgency, so the solver always
prefers V1's zero protocol fee over V2's paid Fast Transfer). Rebuilt from
scratch 2026-09-22 after the previous connectors/cctp.py (gas-ESTIMATION
only, no execution — see its ghost in git history at commit b456def) was
deleted in fb294ce and never replaced.

Scope: ARBITRUM -> SOLANA only, the one CCTP route this project actually
uses (the withdraw pipeline into the Solana vault, see
graph.structures.bridges._CCTP_CHAINS and Graph._linkWalletPayouts) — not a
general N-chain CCTP library. Every address/program ID/PDA seed below was
cross-checked against Circle's own circlefin/solana-cctp-contracts repo
(programs/message-transmitter/src, programs/token-messenger-minter/src,
examples/utils.ts, examples/receiveMessage.ts, examples/target/idl/*.json)
and developers.circle.com/cctp/v1/* on 2026-09-22, not guessed — see each
constant's own comment for its specific source. Getting one of these WRONG
is not, on its own, a fund-loss risk: every account this module derives is
independently validated on-chain by the CCTP programs themselves (an Anchor
`#[account(seeds = [...], bump)]` constraint simply REJECTS the instruction
if a derived PDA doesn't match what the program recomputes) — a mistake
here fails the transaction, it does not misdirect funds. The one exception,
where a mistake WOULD misdirect funds, is `mint_recipient` (see
encode_mint_recipient_for_solana below): that's an opaque bytes32 the
programs trust rather than re-derive, so it deserves its own extra care at
the call site.

This module is the protocol layer only (calldata/instruction encoding, PDA
derivation, attestation polling) — deliberately wallet/web3-agnostic so it
never imports compass_test (the reverse already holds throughout connectors/
— see e.g. connectors/solana_rpc.py). The actual signing/sending glue
(OperatingWallet, SolanaWallet, compass_test.chain_ops) lives in
compass_test/runners/cctp.py, same split as every other DEX connector here.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

import base58
import requests
from eth_utils import keccak
from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey

from graph.structures.DEXes import Chain

from .exceptions import ConnectorAPIError, UnsupportedChainError
from .solana_rpc import SOLANA_USDC_MINT

# --- Domains (Circle's own chain registry, independent of protocol version
# -- shared by V1 and V2) -----------------------------------------------
# Verified: developers.circle.com/cctp/v1/evm-smart-contracts (Arbitrum) and
# circlefin/solana-cctp-contracts/examples/utils.ts's SOLANA_SRC_DOMAIN_ID=5
# (Solana), 2026-09-22.
CCTP_DOMAIN_BY_CHAIN: dict[Chain, int] = {
    Chain.ARBITRUM: 3,
    Chain.SOLANA: 5,
}

# --- EVM side (Arbitrum) -------------------------------------------------
# "Circle: Token Messenger" on Arbiscan, cross-checked against
# arbiscan.io/address/0x19330d10D9Cc8751218eaf51E8885D058642E08A 2026-09-22
# (same address the pre-deletion connectors/cctp.py already had for every
# EVM chain it covered — this one specifically re-verified, not just
# inherited, since it's the one this project actually calls).
ARBITRUM_TOKEN_MESSENGER_V1_ADDRESS = "0x19330d10D9Cc8751218eaf51E8885D058642E08A"

# Selector of depositForBurn(uint256,uint32,bytes32,address) =
# keccak256(signature)[:4] — computed at import time rather than hardcoded,
# so the derivation is auditable instead of trusted blind (this is the one
# EVM-side constant where getting it wrong is caught immediately: an eth
# call to an unknown selector just reverts, no funds at risk from the
# selector itself).
_DEPOSIT_FOR_BURN_SIGNATURE = "depositForBurn(uint256,uint32,bytes32,address)"
DEPOSIT_FOR_BURN_SELECTOR = keccak(text=_DEPOSIT_FOR_BURN_SIGNATURE)[:4]

TOKEN_MESSENGER_ABI = [
    {
        "inputs": [
            {"name": "amount", "type": "uint256"},
            {"name": "destinationDomain", "type": "uint32"},
            {"name": "mintRecipient", "type": "bytes32"},
            {"name": "burnToken", "type": "address"},
        ],
        "name": "depositForBurn",
        "outputs": [{"name": "_nonce", "type": "uint64"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

# MessageTransmitter's MessageSent(bytes message) event — emitted on the
# SOURCE chain's tx receipt during depositForBurn (TokenMessenger calls into
# MessageTransmitter internally); this is what we scrape the CCTP message
# bytes from, keyed by hash rather than by which contract emitted it (we
# never need MessageTransmitter's own Arbitrum address for this reason).
_MESSAGE_SENT_SIGNATURE = "MessageSent(bytes)"
MESSAGE_SENT_TOPIC = "0x" + keccak(text=_MESSAGE_SENT_SIGNATURE).hex()


def encode_mint_recipient_for_solana(solana_token_account_address: str) -> bytes:
    """The bytes32 `mintRecipient` depositForBurn expects, for a SOLANA
    destination specifically: a Solana address (32 bytes already, base58)
    used AS-IS, not left-padded the way an EVM (20-byte) address is
    elsewhere in CCTP. This IS the recipient the mint on Solana trusts
    outright — the programs never re-derive or validate it against
    anything else (unlike every PDA in this module) — so a wrong value
    here really does send the minted USDC to whatever account those bytes
    happen to decode to, not to an error. Per circlefin/solana-cctp-
    contracts/examples/README.md ("this address must be decoded from
    base58 to hex first"), it must be a SPL TOKEN ACCOUNT (e.g. the
    recipient's USDC Associated Token Account), never the owner wallet's
    own pubkey — CCTP mints directly into a token account, it does not
    resolve an owner's ATA for you. That account must already exist
    on-chain before the receiveMessage leg runs, or the mint CPI fails."""
    decoded = base58.b58decode(solana_token_account_address)
    if len(decoded) != 32:
        raise ValueError(
            f"{solana_token_account_address!r} does not decode to 32 bytes "
            f"(got {len(decoded)}) — not a valid Solana address"
        )
    return decoded


def encode_deposit_for_burn_calldata(amount_units: int, destination_domain: int, mint_recipient: bytes, burn_token_address: str) -> bytes:
    """Raw calldata for TokenMessenger.depositForBurn — amount_units is in
    the burn token's own on-chain units (see compass_test.chain_ops.
    usd_to_token_units for USDC's 6 decimals on Arbitrum), not USD."""
    if len(mint_recipient) != 32:
        raise ValueError(f"mint_recipient must be exactly 32 bytes, got {len(mint_recipient)}")
    burn_token_bytes = bytes.fromhex(burn_token_address.removeprefix("0x"))
    if len(burn_token_bytes) != 20:
        raise ValueError(f"burn_token_address must be a 20-byte EVM address, got {burn_token_address!r}")
    return (
        DEPOSIT_FOR_BURN_SELECTOR
        + amount_units.to_bytes(32, "big")
        + destination_domain.to_bytes(32, "big")
        + mint_recipient
        + burn_token_bytes.rjust(32, b"\x00")
    )


def extract_message_from_receipt(receipt) -> bytes:
    """Pulls the CCTP message bytes out of a depositForBurn transaction
    receipt's logs (web3.py TxReceipt) — the payload receiveMessage/the
    attestation service both key off of, not the receipt itself. The
    MessageSent event has a single non-indexed `bytes message` argument,
    so its `data` is the standard ABI dynamic-bytes encoding: a 32-byte
    offset (always 0x20 for one param), a 32-byte length, then the bytes
    themselves — decoded by hand here rather than via a full contract ABI
    object, since topic0 alone already tells us unambiguously which event
    this is."""
    for log in receipt["logs"]:
        topics = log["topics"]
        if topics and topics[0].hex().removeprefix("0x") == MESSAGE_SENT_TOPIC.removeprefix("0x"):
            data = bytes(log["data"])
            length = int.from_bytes(data[32:64], "big")
            return data[64 : 64 + length]
    raise ValueError(
        f"no MessageSent event (topic {MESSAGE_SENT_TOPIC}) found in tx {receipt['transactionHash'].hex()}'s logs "
        "— was this really a depositForBurn receipt?"
    )


def message_hash(message: bytes) -> str:
    return "0x" + keccak(message).hex()


# --- Attestation (Circle's Iris service) ---------------------------------
IRIS_API_URL_MAINNET = "https://iris-api.circle.com"
IRIS_API_URL_SANDBOX = "https://iris-api-sandbox.circle.com"


@dataclass
class Attestation:
    status: str  # "complete" | "pending_confirmation" | ...
    attestation: bytes | None  # None until status == "complete"


def get_attestation(message: bytes, *, mainnet: bool = True) -> Attestation:
    """One poll of GET /v1/attestations/{messageHash} — verified against
    developers.circle.com/api-reference/cctp/all/get-attestation 2026-09-22.
    Never blocks; see poll_attestation for the waiting loop."""
    base_url = IRIS_API_URL_MAINNET if mainnet else IRIS_API_URL_SANDBOX
    response = requests.get(f"{base_url}/v1/attestations/{message_hash(message)}", timeout=15)
    if not response.ok:
        raise ConnectorAPIError("cctp.attestation", base_url, response.text)
    data = response.json()
    attestation_hex = data.get("attestation")
    return Attestation(
        status=data.get("status", "unknown"),
        attestation=bytes.fromhex(attestation_hex.removeprefix("0x")) if attestation_hex else None,
    )


def poll_attestation(message: bytes, *, mainnet: bool = True, timeout_s: float = 1200.0, interval_s: float = 5.0) -> bytes:
    """Blocks until Circle's attestation for `message` is ready (V1/Standard
    Transfer on Arbitrum: ~13-19 min per Circle's own finality docs, hence
    the generous default timeout — this is NOT a fast path, callers should
    surface progress via their own on_stage callback rather than let this
    look hung). Raises TimeoutError rather than returning None past the
    deadline, so a caller can't accidentally treat "not ready yet" as "no
    attestation exists"."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        result = get_attestation(message, mainnet=mainnet)
        if result.status == "complete" and result.attestation is not None:
            return result.attestation
        time.sleep(interval_s)
    raise TimeoutError(f"attestation for {message_hash(message)} not ready after {timeout_s}s")


# --- Solana side -----------------------------------------------------------
# Program IDs verified two ways: developers.circle.com/cctp/v1/solana-
# programs AND the `address` field baked into circlefin/solana-cctp-
# contracts/examples/target/idl/{message_transmitter,token_messenger_minter}
# _031.json (Anchor embeds a program's own deployed address in its IDL) —
# both agreed, 2026-09-22. Same IDs on devnet and mainnet-beta (Circle
# deploys CCTP V1 identically on both clusters).
SOLANA_MESSAGE_TRANSMITTER_PROGRAM_ID = Pubkey.from_string("CCTPmbSD7gX1bxKPAmg77w8oFzNFpaQiQUWD43TKaecd")
SOLANA_TOKEN_MESSENGER_MINTER_PROGRAM_ID = Pubkey.from_string("CCTPiPYPc6AsJuwueEnWgSgucamXDZwBd53dQ11YiKX3")
SOLANA_USDC_MINT_PUBKEY = Pubkey.from_string(SOLANA_USDC_MINT)
SOLANA_TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
# Verified against spl.solana.com/associated-token-account, 2026-09-22.
SOLANA_ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")


def get_usdc_associated_token_account(owner_address: str) -> Pubkey:
    """The canonical (deterministic) USDC Associated Token Account for a
    Solana wallet owner — this is what `mint_recipient` should almost always
    actually be (see encode_mint_recipient_for_solana), NOT the owner
    address itself. Derivation only: this does NOT check the account
    exists on-chain, let alone create it — receiveMessage's mint CPI will
    simply fail if it doesn't exist yet (see this module's docstring: a
    wrong/missing account fails safe, it doesn't misdirect funds)."""
    owner = Pubkey.from_string(owner_address) if isinstance(owner_address, str) else owner_address
    return _find_pda(
        [bytes(owner), bytes(SOLANA_TOKEN_PROGRAM_ID), bytes(SOLANA_USDC_MINT_PUBKEY)],
        SOLANA_ASSOCIATED_TOKEN_PROGRAM_ID,
    )

# UsedNonces::MAX_NONCES (programs/message-transmitter/src/state.rs) — how
# many nonces one on-chain UsedNonces bucket account covers; needed to
# compute which bucket a given nonce falls in (see first_nonce_bucket).
_MAX_NONCES_PER_BUCKET = 6400


def _find_pda(seeds: list[bytes], program_id: Pubkey) -> Pubkey:
    pda, _bump = Pubkey.find_program_address(seeds, program_id)
    return pda


def first_nonce_bucket(nonce: int) -> int:
    """Port of UsedNonces::first_nonce (programs/message-transmitter/src/
    state.rs) — which nonce a message's `used_nonces` PDA is seeded with is
    NOT the message's own nonce, it's the first nonce of its 6400-wide
    bucket (nonces are recorded as a bitset per bucket to save on-chain
    space, see that struct's own doc comment). Reimplemented here (integer
    math only) rather than resolved via an on-chain `getNoncePda` view call
    like Circle's own TS example does, since the source is this simple and
    stable — a wrong result here still just fails the receive_message tx
    (see this module's docstring), it doesn't misdirect anything."""
    if nonce == 0:
        raise ValueError("nonce must be > 0")
    return ((nonce - 1) // _MAX_NONCES_PER_BUCKET) * _MAX_NONCES_PER_BUCKET + 1


def used_nonces_seed_delimiter(source_domain: int) -> bytes:
    """Port of UsedNonces::used_nonces_seed_delimiter — empty for every
    domain this module actually uses (Arbitrum=3), only non-empty
    (b"-") for domain >= 11, added post-mainnet-launch for newer domains
    without disturbing existing PDAs. Kept general rather than hardcoded to
    b"" so a future domain >= 11 doesn't silently derive the wrong PDA."""
    return b"" if source_domain < 11 else b"-"


@dataclass
class DecodedCctpMessage:
    version: int
    source_domain: int
    destination_domain: int
    nonce: int
    sender: bytes  # 32 bytes
    recipient: bytes  # 32 bytes
    destination_caller: bytes  # 32 bytes
    message_body: bytes


# Byte offsets from programs/message-transmitter/src/message.rs's Message
# impl (VERSION_INDEX..MESSAGE_BODY_INDEX) — the CCTP V1 wire format,
# identical on every chain (it's chain-agnostic on the wire; only how each
# chain's contract PRODUCES/CONSUMES it differs). All fields big-endian.
def decode_cctp_message(message: bytes) -> DecodedCctpMessage:
    if len(message) < 116:
        raise ValueError(f"message too short to be a CCTP V1 message ({len(message)} bytes, need >= 116)")
    return DecodedCctpMessage(
        version=int.from_bytes(message[0:4], "big"),
        source_domain=int.from_bytes(message[4:8], "big"),
        destination_domain=int.from_bytes(message[8:12], "big"),
        nonce=int.from_bytes(message[12:20], "big"),
        sender=message[20:52],
        recipient=message[52:84],
        destination_caller=message[84:116],
        message_body=message[116:],
    )


@dataclass
class ReceiveMessagePdas:
    token_messenger: Pubkey
    remote_token_messenger: Pubkey
    token_minter: Pubkey
    local_token: Pubkey
    token_pair: Pubkey
    custody_token_account: Pubkey
    authority_pda: Pubkey
    used_nonces: Pubkey
    message_transmitter: Pubkey
    token_messenger_event_authority: Pubkey


def get_receive_message_pdas(source_domain: int, remote_token_evm_address: str, nonce: int) -> ReceiveMessagePdas:
    """Every PDA receive_message needs, derived exactly like circlefin/
    solana-cctp-contracts/examples/utils.ts::getReceiveMessagePdas (seeds
    cross-checked against the Rust source directly, see each field below).
    `remote_token_evm_address` is the BURNED token's address on the source
    chain (Arbitrum USDC, see connectors.stable_tokens) — token_pair keys
    off of it left-padded to 32 bytes as a Solana Pubkey, the same
    left-pad convention EVM-side bytes32 encoding already uses elsewhere in
    CCTP (this is @solana/web3.js's PublicKey(Buffer) constructor treating
    a <32-byte buffer as a big-endian number and re-serializing it to 32
    bytes, which is numerically identical to a left-pad)."""
    tmm = SOLANA_TOKEN_MESSENGER_MINTER_PROGRAM_ID
    mt = SOLANA_MESSAGE_TRANSMITTER_PROGRAM_ID
    domain_bytes = str(source_domain).encode()

    remote_token_raw = bytes.fromhex(remote_token_evm_address.removeprefix("0x"))
    remote_token_padded = remote_token_raw.rjust(32, b"\x00")

    first_nonce = first_nonce_bucket(nonce)

    return ReceiveMessagePdas(
        token_messenger=_find_pda([b"token_messenger"], tmm),
        remote_token_messenger=_find_pda([b"remote_token_messenger", domain_bytes], tmm),
        token_minter=_find_pda([b"token_minter"], tmm),
        local_token=_find_pda([b"local_token", bytes(SOLANA_USDC_MINT_PUBKEY)], tmm),
        token_pair=_find_pda([b"token_pair", domain_bytes, remote_token_padded], tmm),
        custody_token_account=_find_pda([b"custody", bytes(SOLANA_USDC_MINT_PUBKEY)], tmm),
        authority_pda=_find_pda([b"message_transmitter_authority", bytes(tmm)], mt),
        used_nonces=_find_pda(
            [b"used_nonces", domain_bytes, used_nonces_seed_delimiter(source_domain), str(first_nonce).encode()],
            mt,
        ),
        message_transmitter=_find_pda([b"message_transmitter"], mt),
        token_messenger_event_authority=_find_pda([b"__event_authority"], tmm),
    )


def _anchor_discriminator(instruction_name: str) -> bytes:
    return hashlib.sha256(f"global:{instruction_name}".encode()).digest()[:8]


def _borsh_bytes(data: bytes) -> bytes:
    return len(data).to_bytes(4, "little") + data


def build_receive_message_instruction(
    *,
    payer: Pubkey,
    message: bytes,
    attestation: bytes,
    user_token_account: Pubkey,
    pdas: ReceiveMessagePdas,
) -> Instruction:
    """The full receive_message instruction — account order and the
    #[event_cpi]-injected trailing (event_authority, program) pair verified
    against programs/message-transmitter/src/instructions/receive_message.rs
    and examples/target/idl/message_transmitter_031.json's own instruction
    definition; the 10 `remainingAccounts` (passed straight through as CPI
    accounts to TokenMessengerMinter.handle_receive_message) verified
    against examples/receiveMessage.ts, both 2026-09-22. `payer` doubles as
    `caller` (see the Context: both are plain Signers, nothing requires two
    distinct wallets) — the destination_caller check in receive_message.rs
    only applies if the ORIGINAL depositForBurn set a nonzero
    destinationCaller, which build_deposit_for_burn_calldata never does."""
    discriminator = _anchor_discriminator("receive_message")
    data = discriminator + _borsh_bytes(message) + _borsh_bytes(attestation)

    named_accounts = [
        AccountMeta(payer, is_signer=True, is_writable=True),  # payer
        AccountMeta(payer, is_signer=True, is_writable=False),  # caller
        AccountMeta(pdas.authority_pda, is_signer=False, is_writable=False),
        AccountMeta(pdas.message_transmitter, is_signer=False, is_writable=False),
        AccountMeta(pdas.used_nonces, is_signer=False, is_writable=True),
        AccountMeta(SOLANA_TOKEN_MESSENGER_MINTER_PROGRAM_ID, is_signer=False, is_writable=False),  # receiver
        AccountMeta(Pubkey.from_string("11111111111111111111111111111111"), is_signer=False, is_writable=False),  # system_program
        AccountMeta(_find_pda([b"__event_authority"], SOLANA_MESSAGE_TRANSMITTER_PROGRAM_ID), is_signer=False, is_writable=False),  # event_authority (MessageTransmitter's own)
        AccountMeta(SOLANA_MESSAGE_TRANSMITTER_PROGRAM_ID, is_signer=False, is_writable=False),  # program
    ]
    remaining_accounts = [
        AccountMeta(pdas.token_messenger, is_signer=False, is_writable=False),
        AccountMeta(pdas.remote_token_messenger, is_signer=False, is_writable=False),
        AccountMeta(pdas.token_minter, is_signer=False, is_writable=True),
        AccountMeta(pdas.local_token, is_signer=False, is_writable=True),
        AccountMeta(pdas.token_pair, is_signer=False, is_writable=False),
        AccountMeta(user_token_account, is_signer=False, is_writable=True),
        AccountMeta(pdas.custody_token_account, is_signer=False, is_writable=True),
        AccountMeta(SOLANA_TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(pdas.token_messenger_event_authority, is_signer=False, is_writable=False),
        AccountMeta(SOLANA_TOKEN_MESSENGER_MINTER_PROGRAM_ID, is_signer=False, is_writable=False),
    ]

    return Instruction(
        SOLANA_MESSAGE_TRANSMITTER_PROGRAM_ID,
        data,
        named_accounts + remaining_accounts,
    )
