"""
Unit tests for the deterministic (non-LLM) parsing helpers that back batch
wallet sends in chat -- see ChatEngine._resolve_batch_endpoints and the
module-level _extract_addresses / _extract_from_wallet_labels in
backend/planner/chat_engine.py.
"""
from __future__ import annotations

from backend.planner.chat_engine import _extract_addresses, _extract_from_wallet_labels

ADDR_1 = "0x" + "1" * 40
ADDR_2 = "0x" + "2" * 40
ADDR_3 = "0x" + "3" * 40


def test_extract_addresses_numbered_list():
    text = f"1- {ADDR_1}\n2-{ADDR_2}\n3 - {ADDR_3}"
    assert _extract_addresses(text) == [ADDR_1, ADDR_2, ADDR_3]


def test_extract_addresses_dedupes_and_preserves_order():
    text = f"send to {ADDR_2} then {ADDR_1} then {ADDR_2} again"
    assert _extract_addresses(text) == [ADDR_2, ADDR_1]


def test_extract_addresses_none_present():
    assert _extract_addresses("send 0.001 to wallet 1") == []


def test_extract_from_wallet_labels_matches_named_senders():
    hot_signers = [
        {"address": ADDR_1, "label": "wallet 1"},
        {"address": ADDR_2, "label": "wallet 2"},
        {"address": ADDR_3, "label": "wallet 3"},
    ]
    text = "wallet 1, wallet 2 theke 0xabc... e pathao"
    assert _extract_from_wallet_labels(text, hot_signers) == [ADDR_1, ADDR_2]


def test_extract_from_wallet_labels_respects_mention_order_not_list_order():
    hot_signers = [
        {"address": ADDR_1, "label": "wallet 1"},
        {"address": ADDR_2, "label": "wallet 2"},
    ]
    text = "from wallet 2 and wallet 1, send 0.01"
    assert _extract_from_wallet_labels(text, hot_signers) == [ADDR_2, ADDR_1]


def test_extract_from_wallet_labels_no_match_returns_empty():
    hot_signers = [{"address": ADDR_1, "label": "wallet 1"}]
    assert _extract_from_wallet_labels("send 0.01 to some address", hot_signers) == []


def test_extract_from_wallet_labels_word_boundary_avoids_partial_match():
    # "wallet 1" should not match inside "wallet 10" or "wallet 12"
    hot_signers = [{"address": ADDR_1, "label": "wallet 1"}]
    assert _extract_from_wallet_labels("wallet 12 theke pathao", hot_signers) == []
    assert _extract_from_wallet_labels("wallet 1 theke pathao", hot_signers) == [ADDR_1]
