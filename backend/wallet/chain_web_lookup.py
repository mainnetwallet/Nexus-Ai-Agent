"""
Web lookup module for unknown EVM chain parameters.

When a user names an unknown chain that is not in hardcoded SUPPORTED_CHAINS
or the local ethereum-lists/chains registry, this module uses web search / HTTP lookup
to discover potential Chain ID and HTTPS RPC endpoint candidates.

The candidate is NEVER auto-executed; it must be confirmed by the human user first.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger("nexus.wallet.chain_web_lookup")


@dataclass
class WebChainCandidate:
    chain_key: str
    display_name: str
    chain_id_int: int
    chain_id_hex: str
    rpc_candidates: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)


async def web_lookup_chain(chain_name: str) -> Optional[WebChainCandidate]:
    """
    Attempts to search public sources / RPC aggregators for an unlisted EVM chain.
    Returns a WebChainCandidate proposal if chain ID and RPC endpoints are found,
    or None if lookup fails.
    """
    key = chain_name.strip().lower()
    logger.info("Initiating web lookup for unknown chain: %s", key)

    # Lookup against DefiLlama chains directory API
    url = "https://api.llama.fi/chains"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                for item in data:
                    c_name = str(item.get("name", "")).lower()
                    token_symbol = str(item.get("tokenSymbol", "")).lower()
                    if key in (c_name, token_symbol) or c_name == key:
                        chain_id = item.get("chainId")
                        if chain_id and isinstance(chain_id, (int, float)):
                            cid = int(chain_id)
                            rpcs = item.get("rpc") or []
                            clean_rpcs = [r for r in rpcs if isinstance(r, str) and r.startswith("https://") and "${" not in r]
                            return WebChainCandidate(
                                chain_key=key.replace(" ", "-"),
                                display_name=item.get("name", chain_name.title()),
                                chain_id_int=cid,
                                chain_id_hex=hex(cid),
                                rpc_candidates=clean_rpcs,
                                source_urls=["https://defillama.com/chains"],
                            )
    except Exception as exc:
        logger.warning("Web lookup error via DefiLlama for %s: %s", key, exc)

    # Generic search fallback using public chainid dataset
    try:
        search_url = "https://chainid.network/chains.json"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(search_url)
            if resp.status_code == 200:
                chains = resp.json()
                for c in chains:
                    name_match = key in str(c.get("name", "")).lower() or key in str(c.get("shortName", "")).lower()
                    if name_match:
                        cid = c.get("chainId")
                        if cid and isinstance(cid, int):
                            rpcs = [
                                r for r in c.get("rpc", [])
                                if isinstance(r, str) and r.startswith("https://") and "${" not in r
                            ]
                            if rpcs:
                                return WebChainCandidate(
                                    chain_key=key.replace(" ", "-"),
                                    display_name=c.get("name", chain_name.title()),
                                    chain_id_int=cid,
                                    chain_id_hex=hex(cid),
                                    rpc_candidates=rpcs,
                                    source_urls=["https://chainid.network"],
                                )
    except Exception as exc:
        logger.warning("Web lookup error via Chainlist for %s: %s", key, exc)

    return None
