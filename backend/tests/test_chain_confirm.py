import pytest
from backend.wallet.chain_web_lookup import WebChainCandidate
from backend.wallet.chain_confirm import ChainConfirmationManager, PendingChainConfirmation
from backend.wallet.chains import chain_by_key, SUPPORTED_CHAINS

def test_chain_confirmation_manager_lifecycle():
    manager = ChainConfirmationManager()
    session_id = "test-session-123"

    assert not manager.is_active(session_id)

    candidate = WebChainCandidate(
        chain_key="test-chain",
        display_name="Test Chain",
        chain_id_int=999999,
        chain_id_hex="0xf423f",
        rpc_candidates=["https://rpc.testchain.org"],
        source_urls=["https://testchain.org"],
    )

    intent = {"wallet_action": "send_native", "send_chain": "test-chain", "send_amount": "0.1"}
    text = "send 0.1 to 0x1111111111111111111111111111111111111111 on test-chain"

    draft = manager.start(session_id, candidate, intent, text)
    assert manager.is_active(session_id)
    assert manager.get_pending(session_id) == draft

    confirmed = manager.pop_confirmed(session_id)
    assert confirmed == draft
    assert not manager.is_active(session_id)

def test_chain_confirmation_manager_cancel():
    manager = ChainConfirmationManager()
    session_id = "test-session-456"

    candidate = WebChainCandidate(
        chain_key="test-chain-2",
        display_name="Test Chain 2",
        chain_id_int=888888,
        chain_id_hex="0xd9038",
        rpc_candidates=["https://rpc.testchain2.org"],
    )

    manager.start(session_id, candidate, {}, "test")
    assert manager.is_active(session_id)

    cancelled = manager.cancel(session_id)
    assert cancelled
    assert not manager.is_active(session_id)
