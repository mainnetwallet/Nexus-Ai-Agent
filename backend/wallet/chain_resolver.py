"""
Dynamic EVM chain resolution + RPC fallback.

Two things this module does:

1. RPC fallback: for chains we already know about (backend/wallet/chains.py),
   `get_rpc_candidates()` returns the official RPC plus a curated list of
   reputable public fallbacks (settings.rpc_endpoints_with_fallback). Callers
   try them in order and move on the first time one errors out.

2. Unknown-chain resolution: if the user names an EVM chain that isn't in
   SUPPORTED_CHAINS (e.g. "avalanche", "fantom", "gnosis"), `resolve_chain()`
   looks it up against the ethereum-lists/chains registry -- the same
   community-maintained, widely-trusted dataset that powers chainlist.org
   and MetaMask's "add network" search. This is deliberately NOT a raw web
   search: an autonomous agent that signs and broadcasts real transactions
   should only pick up chain id / RPC data from a source with some
   editorial/community vetting, not whatever a search engine returns first.

   Resolved chains are cached in-memory for the process lifetime via
   chains.register_dynamic_chain() so we don't re-fetch on every message.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from backend.config.settings import settings
from backend.wallet.chains import ChainInfo, chain_by_key, register_dynamic_chain

logger = logging.getLogger(__name__)

# Name/alias -> canonical EVM chain id, for chains the agent doesn't have
# hardcoded in chains.py. Covers the common ones people are likely to name
# in chat; anything else can still be resolved by passing the numeric chain
# id directly (e.g. "chain 250").
_ALIAS_TO_CHAIN_ID: dict[str, int] = {
    "avalanche": 43114, "avax": 43114, "avalanche c-chain": 43114,
    "fantom": 250, "ftm": 250, "fantom opera": 250,
    "gnosis": 100, "gnosis chain": 100, "xdai": 100,
    "celo": 42220,
    "moonbeam": 1284,
    "moonriver": 1285,
    "cronos": 25,
    "harmony": 1666600000,
    "aurora": 1313161554,
    "metis": 1088, "metis andromeda": 1088,
    "kava": 2222,
    "canto": 7700,
    "linea": 59144,
    "scroll": 534352,
    "zksync": 324, "zksync era": 324,
    "mantle": 5000,
    "blast": 81457,
    "mode": 34443,
    "zora": 7777777,
    "polygon zkevm": 1101, "zkevm": 1101,
    "opbnb": 204,
    "fraxtal": 252,
    "sei": 1329,
    "taiko": 167000,
}

_REGISTRY_URL = "https://raw.githubusercontent.com/ethereum-lists/chains/master/_data/chains/eip155-{chain_id}.json"


def _clean_rpc_list(raw_rpcs: list[str]) -> list[str]:
    """Keep only plain https public endpoints -- drop ws(s), and drop any
    that need an API key we don't have (e.g. '${INFURA_API_KEY}')."""
    out = []
    for url in raw_rpcs:
        if not url.startswith("https://"):
            continue
        if "${" in url:
            continue
        out.append(url.rstrip("/"))
    return out


async def _fetch_chain_from_registry(chain_id: int) -> Optional[dict]:
    url = _REGISTRY_URL.format(chain_id=chain_id)
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
    except Exception:
        logger.exception("chain_resolver: registry lookup failed for chain id %s", chain_id)
        return None


async def resolve_chain(name_or_id: str) -> tuple[Optional[ChainInfo], list[str]]:
    """
    Resolve a chain the user names in chat to (ChainInfo, rpc_candidates).

    Order: hardcoded SUPPORTED_CHAINS/dynamic cache -> alias table -> numeric
    chain id -> ethereum-lists/chains registry. Returns (None, []) if nothing
    matched, so callers can surface a clear "unsupported chain" error instead
    of guessing.
    """
    key = name_or_id.strip().lower()

    known = chain_by_key(key)
    if known:
        return known, get_rpc_candidates(known)

    chain_id: Optional[int] = _ALIAS_TO_CHAIN_ID.get(key)
    if chain_id is None and key.isdigit():
        chain_id = int(key)
    if chain_id is None and key.startswith("0x"):
        try:
            chain_id = int(key, 16)
        except ValueError:
            chain_id = None

    if chain_id is None:
        return None, []

    data = await _fetch_chain_from_registry(chain_id)
    if not data:
        return None, []

    rpc_candidates = _clean_rpc_list(data.get("rpc", []))
    if not rpc_candidates:
        # No usable public RPC for this chain -- can't safely proceed.
        return None, []

    info = ChainInfo(
        key=key.replace(" ", "-"),
        display_name=data.get("name", key.title()),
        chain_id_hex=hex(chain_id),
        chain_id_int=chain_id,
    )
    register_dynamic_chain(info)
    logger.info(
        "chain_resolver: dynamically resolved %r -> %s (chainId=%s, %d rpc candidates)",
        name_or_id, info.display_name, chain_id, len(rpc_candidates),
    )
    return info, rpc_candidates


def get_rpc_candidates(chain: ChainInfo) -> list[str]:
    """Official + fallback RPC list for a known/hardcoded chain."""
    fallback_map = settings.rpc_endpoints_with_fallback
    candidates = fallback_map.get(chain.key.lower())
    if candidates:
        return candidates
    primary = settings.rpc_endpoints.get(chain.key.lower())
    return [primary] if primary else []


async def rpc_post_with_fallback(rpc_candidates: list[str], payload: dict, timeout: float = 10.0) -> dict:
    """
    POST a JSON-RPC payload, trying each candidate URL in order until one
    responds successfully. Raises the last error if all fail.

    Each candidate gets one immediate retry before moving on -- endpoints
    (Alchemy in particular) occasionally 400/5xx a single request that
    succeeds a moment later on an identical retry (observed: same exact
    call succeeding when re-sent manually seconds after the app saw a
    400 from it), so this catches that transient case before falling
    back to a different, possibly-lower-quality node.

    Exception: if a candidate returns a deterministic, on-chain-state error
    (e.g. "insufficient funds", "nonce too low") rather than an
    endpoint-level problem (rate limit, auth, network), we stop immediately
    instead of burning through the rest of the fallback list -- every other
    RPC node will report the same on-chain state, so retrying elsewhere
    can't fix it and only hides the real error behind whatever the last
    endpoint happened to say (e.g. a 429).
    """
    last_error: Optional[Exception] = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for rpc_url in rpc_candidates:
            for attempt in (1, 2):
                try:
                    resp = await client.post(rpc_url, json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                    if "error" in data:
                        raise RuntimeError(f"RPC error from {rpc_url}: {data['error']}")
                    return data
                except Exception as exc:
                    if _is_deterministic_chain_error(exc):
                        logger.warning(
                            "chain_resolver: RPC candidate %s returned a deterministic chain error (%s), "
                            "not trying further fallbacks", rpc_url, exc,
                        )
                        raise
                    last_error = exc
                    if attempt == 1:
                        logger.warning(
                            "chain_resolver: RPC candidate %s failed (%s), retrying once before "
                            "moving on", rpc_url, exc,
                        )
                    else:
                        logger.warning(
                            "chain_resolver: RPC candidate %s failed again (%s), trying next", rpc_url, exc,
                        )
    raise last_error or RuntimeError("No RPC candidates available")


_DETERMINISTIC_CHAIN_ERROR_MARKERS = (
    "insufficient funds",
    "nonce too low",
    "already known",
    "replacement transaction underpriced",
)


def _is_deterministic_chain_error(exc: Exception) -> bool:
    """True if `exc` reflects on-chain state (same on every node) rather than
    an endpoint-specific problem worth falling back away from."""
    text = str(exc).lower()
    return any(marker in text for marker in _DETERMINISTIC_CHAIN_ERROR_MARKERS)
