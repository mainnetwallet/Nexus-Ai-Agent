"""
Conversational AI Chat engine.

Turns Nexus-Agent from a task-only agent into something you can just talk
to. This module owns exactly one thing: classify a free-form message into
one of seven categories, then dispatch it to whichever existing module
already owns that behavior. It never reimplements task execution, agent
lifecycle, browser observation, or reporting -- it only composes
TaskQueueService, AgentRuntime, LiveSessionManager and the Report/Task
tables, all of which already exist.

Used by both the dashboard's AI Chat page (backend/api/routes_chat.py) and
the Telegram bot's natural-language fallback (backend/telegram/bot.py), so
"chat with the agent" behaves identically everywhere and there is exactly
one place that owns intent classification for conversation.

Categories (see CLASSIFIER_SYSTEM_PROMPT):
  conversation     - small talk, greetings, "what can you do"
  question         - answerable from context/knowledge, no action needed
  browser_command  - open/search/summarize/screenshot/show current browser
  agent_command     - pause/resume/stop/start/continue the agent or a task
  task             - a new goal-driven task to queue
  settings         - read current configuration
  system_request   - status/history/explain-failure/explain-last-action
  skill            - learn/list/enable/disable/delete a skill, Teach Mode,
                     confirming/discarding a "save as skill?" suggestion, or
                     correcting a skill's learned workflow (see
                     backend/skills/ and ChatEngine._handle_skill_command)
  mcp              - off-page-web/filesystem/terminal/github requests routed
                     through an MCP connector (see backend/mcp/ and
                     ChatEngine._handle_mcp_command)
  ai_model         - switch/default/auto-route/override the active LLM
                     provider, or ask about current provider/model/health
                     (see backend/planner/model_manager.py and
                     ChatEngine._handle_ai_model_command)
  wallet           - start a multi-turn "queue N transactions" batch, OR
                     wallet CRUD (import/list/delete/rename/select-active/
                     balance/network-switch/groups) (see backend/wallet/
                     tx_batch.py + backend/wallet/registry.py and
                     ChatEngine._handle_wallet_command / _handle_batch_turn).
                     Batches only ever queue tasks; wallet approval policy
                     stays Settings-only (see tx_batch.py's module
                     docstring for why).
  profile          - Chrome Profile CRUD (create/list/enable/disable/
                     delete/clone/rename/open-in-chrome/check-sessions/
                     select-active) (see backend/identity/registry.py and
                     ChatEngine._handle_profile_command). Distinct from a
                     task's profile_label, which just picks which existing
                     profile a task runs under.
  memory           - search/list/archive/forget/merge-duplicates/analytics
                     over the agent's long-term memory (see
                     backend/memory/store.py and
                     ChatEngine._handle_memory_command)
  plugin           - list/enable/disable/reload/rescan installed plugins
                     (see backend/plugins/registry.py and
                     ChatEngine._handle_plugin_command)
  system           - health/diagnostics/resources/version/config export,
                     import, backup, restore (see backend/monitoring/,
                     backend/config/config_manager.py and
                     ChatEngine._handle_system_command)

Task management beyond pause/resume/cancel (retry/delete/list/history/
report/priority) and settings WRITES (not just reads) are handled as
extra actions within the existing agent_command / settings categories --
see the "agent_command" and "settings" guidance below.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Optional

from sqlalchemy import select

from backend.config.settings import settings
from backend.database.models import ChatMessage, ChatRole, ChatSession, Report, SkillSource, Task, TaskStatus
from backend.database.session import get_session
from backend.planner.llm_client import LLMClient
from backend.planner.model_manager import TaskType
from backend.planner.model_manager import model_manager as _default_model_manager
from backend.identity.manager import ProfileManager
from backend.identity.pending_profile import PendingTask
from backend.wallet.hot_signer import (
    BatchTransferResult,
    HotSignerDisabled,
    HotSignerError,
    get_hot_signer_address,
    list_hot_signers,
)

logger = logging.getLogger("nexus.chat")

CLASSIFIER_SYSTEM_PROMPT = """You classify a message sent to Nexus-Agent, an autonomous browser-automation \
agent, into a structured intent. Respond with STRICT JSON only, no prose, no markdown fences:

Context handling: the user prompt you receive may start with a "Conversation so far:" block (the most \
recent prior turns of this session) followed by "New message to classify: <text>". When that block is \
present, you MUST use it to resolve the new message before classifying -- pronouns ("it", "that one", \
"the same site"), short replies that only make sense as an answer to your own previous clarifying \
question, and any other implicit continuation of what was already being discussed. Classify the COMBINED \
meaning as a single intent: e.g. if the assistant's last turn asked "What type of page?" in response to \
"Build an HTML page", and the new message is just "An AI dashboard", treat this as one continued request \
(goal combining both: an HTML page for an AI dashboard) rather than classifying "An AI dashboard" alone. \
If there is no "Conversation so far:" block, classify the message by itself exactly as before.
{
  "category": "conversation | question | browser_command | agent_command | task | settings | system_request | skill | mcp | wallet | profile | memory | plugin | system",
  "action": "short action keyword, see guidance below",
  "website": "url if one is mentioned or implied, else empty",
  "goal": "goal description if this describes work to perform, else empty",
  "query": "search text or free-form subject, if relevant, else empty",
  "wallet_label": "wallet label if mentioned, else empty",
  "profile_label": "browser profile name or id if one is mentioned, else empty",
  "task_id": "the specific task id mentioned (e.g. after 'pause task', 'cancel task', 'resume task', \
'retry task', 'delete task', 'prioritize task'), else empty",
  "priority": "integer priority value, only set when action=set_priority, else empty",
  "settings_action": "read | update -- only set when category=settings, default read",
  "settings_field": "the exact settings field name to change (e.g. browser_headless, \
wallet_require_manual_approval, vision_enabled, ocr_enabled, live_session_enabled, \
wallet_max_auto_approve_value_usd, browser_slow_mo_ms), only set when settings_action=update",
  "settings_value": "the new value as a plain string ('true'/'false'/a number/text), only set when \
settings_action=update",
  "profile_action": "list | create | enable | disable | delete | clone | rename | open | sessions | \
activity | select -- only set when category=profile",
  "profile_new_name": "the new name, only set when profile_action=rename or clone",
  "memory_action": "search | list | analytics | archive | unarchive | forget | duplicates | \
merge_duplicates -- only set when category=memory",
  "memory_query": "free-text search/filter subject, only set when memory_action=search or list",
  "memory_id": "a specific memory entry id, only set when memory_action=archive/unarchive/forget",
  "plugin_action": "list | enable | disable | reload | rescan -- only set when category=plugin",
  "plugin_name": "the plugin name, only set when plugin_action=enable/disable/reload",
  "system_action": "health | diagnostics | resources | version | config_export | config_backup | \
config_backups | config_restore -- only set when category=system",
  "system_backup_filename": "a backup filename, only set when system_action=config_restore",
  "skill_action": "learn | confirm | discard | teach_start | teach_finish | teach_cancel | teach_undo | \
list | enable | disable | delete | correct -- only set when category=skill",
  "skill_name": "name (or partial name) of an existing skill this message refers to, else empty",
  "skill_text": "the free-form skill description to learn, teach-mode step text, or correction instruction, \
else empty",
  "mcp_query": "the raw request text describing the filesystem/terminal/github/fetch-URL work to perform, \
only set when category=mcp, else empty",
  "mcp_connector": "filesystem | terminal | browser | github -- only set when the message clearly names \
which connector to use, else empty",
  "mcp_action": "call | list_connectors | enable_connector | disable_connector | health -- only set when \
category=mcp; defaults to 'call' (route the free text to a tool) when omitted",
  "ai_action": "switch | set_default | enable_auto_routing | disable_auto_routing | set_routing_rule | \
temporary_use | show_provider | show_model | show_providers | show_health | show_routing -- only set \
when category=ai_model",
  "ai_provider": "the AI provider named in the message (e.g. claude, gpt, gemini, groq, openrouter, cohere, \
huggingface, mistral, grok, kimi, qwen, glm...), only set when category=ai_model and a provider is named",
  "ai_task_type": "coding | browser_automation | planning | vision | long_context | fast_response | \
general_chat | research | reasoning | low_cost -- only set when category=ai_model action=set_routing_rule",
  "wallet_action": "batch_start | list | import | delete | rename | select | balance | groups_list | \
network_switch | send_native | send_token -- only set when category=wallet",
  "tx_count": "integer number of transactions/payments the user wants queued in a batch (e.g. from \"10 ta \
transaction koro\" / \"queue 10 transactions\" / \"do 5 payments\"), only set when category=wallet \
wallet_action=batch_start, else empty",
  "wallet_new_name": "the new label, only set when wallet_action=rename",
  "wallet_import_method": "address | private_key | seed_phrase -- only set when wallet_action=import; \
NEVER put the actual private key or seed phrase text in this field, or anywhere else in this JSON -- if \
the raw message itself contains what looks like a seed phrase or private key, that is handled separately \
before you ever see it, so you will not be asked to classify a message like that",
  "wallet_address": "a wallet address, only set when wallet_action=import and wallet_import_method=address",
  "wallet_network": "the target network name, only set when wallet_action=network_switch",
  "send_chain": "the chain/network name (ethereum | polygon | arbitrum | optimism | base | bsc), only set \
when wallet_action=send_native or wallet_action=send_token",
  "send_to_address": "the 0x destination address, only set when wallet_action=send_native or \
wallet_action=send_token",
  "send_amount": "the numeric amount to send as a plain string (e.g. \"0.05\"), only set when \
wallet_action=send_native or wallet_action=send_token",
  "send_token_address": "the 0x contract address of the ERC20 token to send, only set when \
wallet_action=send_token. If the user names a token by symbol (e.g. \"USDC\") instead of giving a contract \
address, leave this empty -- do not guess a contract address.",
  "wallet_save_as_hot_signer": "true, only set when wallet_action=import AND the user explicitly asks for \
this wallet to also be usable for direct/no-popup sends (e.g. \"hot signer hisebe set koro\", \"eta diye \
tnx korte parbe\", \"save as hot signer\", \"make this the hot wallet\", \"use this for auto sending\"). \
Leave empty/unset otherwise -- this must never be inferred just because the user is importing a wallet."
}

Guidance:
- Greetings, small talk, "what are you doing", "what can you do" -> category=conversation
- General questions not about a specific action -> category=question
- "open chrome", "open <url>" -> category=browser_command action=open
- "search for X" -> category=browser_command action=search query=X
- "summarize this page" -> category=browser_command action=summarize
- "take a screenshot" -> category=browser_command action=screenshot
- "show browser" / "what's on screen" -> category=browser_command action=show
- "pause" (no task named) -> category=agent_command action=pause
- "resume" (no task named) -> category=agent_command action=resume
- "stop" -> category=agent_command action=stop
- "continue the previous task" / "keep going" -> category=agent_command action=continue
- "pause task" / "pause this task" / "pause the task" / "pause task <id>" -> category=agent_command \
action=pause_task task_id=<id if one was given, else empty>
- "resume task" / "resume this task" / "resume task <id>" -> category=agent_command action=resume_task \
task_id=<id if one was given, else empty>
- "cancel task" / "cancel this task" / "cancel it" / "cancel task <id>" / "stop this task" (referring to \
one specific task, not the whole agent) -> category=agent_command action=cancel_task task_id=<id if one \
was given, else empty>
- "check my current task" / "what's the status" -> category=system_request action=current_task
- "explain why you failed" / "why did that fail" -> category=system_request action=explain_failure
- "explain your last action" / "what did you just do" -> category=system_request action=explain_last_action
- "what happened today" -> category=system_request action=today_summary
- settings/configuration questions ("what model are you using", "what wallet mode") -> category=settings
- "learn how to ...", "remember how to ...", "save this as a skill: ..." -> category=skill \
skill_action=learn skill_text=<the full description of the workflow>
- "save as skill" / "yes save it" / "keep that skill" (answering a "save this as a skill?" prompt) -> \
category=skill skill_action=confirm
- "discard" / "don't save it" / "no, skip it" (answering a "save this as a skill?" prompt) -> \
category=skill skill_action=discard
- "teach me a skill" / "let's do teach mode" / "I want to teach you something" -> category=skill \
skill_action=teach_start skill_name=<name if one was given, else empty>
- "done" / "finish" / "that's it, save it" / "done teaching" (while teaching) -> category=skill \
skill_action=teach_finish
- "cancel" / "cancel teaching" / "forget this one" (while teaching) -> category=skill skill_action=teach_cancel
- "undo" / "undo that step" / "undo the last step" (while teaching) -> category=skill skill_action=teach_undo
- "list my skills" / "what skills do you know" / "show me my skills" -> category=skill skill_action=list
- "enable the X skill" -> category=skill skill_action=enable skill_name=X
- "disable the X skill" -> category=skill skill_action=disable skill_name=X
- "delete the X skill" / "forget the X skill" -> category=skill skill_action=delete skill_name=X
- "no, actually click the confirm button instead" / a correction about a step a learned skill just took -> \
category=skill skill_action=correct skill_name=<skill if named, else empty> skill_text=<the correction>
- "list files in X" / "read file X" / "write/delete/search files in X" -> category=mcp mcp_connector=filesystem \
mcp_query=<the raw request>
- "run command X" / "execute X in the terminal" -> category=mcp mcp_connector=terminal mcp_query=<the raw request>
- "check github issues on X/Y" / "create an issue on X/Y" / "list PRs on X/Y" / "get file contents from X/Y" -> \
category=mcp mcp_connector=github mcp_query=<the raw request>
- "fetch this URL" / "get the links on this page" (off-page, not the live browser session) -> category=mcp \
mcp_connector=browser mcp_query=<the raw request>
- If the message clearly needs filesystem/terminal/github/off-page-web access but doesn't name which one, \
still use category=mcp with mcp_connector left empty so it can be auto-routed
- "list mcp connectors" / "show connectors" -> category=mcp mcp_action=list_connectors
- "enable/disable the X connector" -> category=mcp mcp_action=enable_connector|disable_connector \
mcp_connector=X
- "connector health" / "mcp health" -> category=mcp mcp_action=health
- Anything that describes NEW work to perform on a website (e.g. "go buy a widget on \
example.com", "complete the KYC form on X") -> category=task, with website/goal filled in
- "run this task with Profile-01" / "use my Profile-01 profile" / "as Profile-01" (naming a \
browser identity/profile, not a wallet) -> category=task, with profile_label filled in alongside \
website/goal/wallet_label
- Only classify as "task" when the message actually describes work to perform. Simple \
conversation, questions, or commands about existing tasks must NOT be classified as task.
- "switch to Claude" / "switch to Gemini" / "use GPT" / "use Groq" -> category=ai_model ai_action=switch \
ai_provider=<the named provider>
- "set Gemini as default" / "make Claude my default provider" -> category=ai_model ai_action=set_default \
ai_provider=<the named provider>
- "use automatic routing" / "enable smart routing" / "turn on auto model routing" -> category=ai_model \
ai_action=enable_auto_routing
- "turn off smart routing" / "use manual mode" / "stop auto-routing" -> category=ai_model \
ai_action=disable_auto_routing
- "use Claude for coding" / "route vision tasks to Gemini" / "use Groq for fast responses" -> category=ai_model \
ai_action=set_routing_rule ai_provider=<the named provider> ai_task_type=<the matching task type>
- "use Claude for this task only" / "use Gemini just this time" / "use Groq for this request" -> \
category=ai_model ai_action=temporary_use ai_provider=<the named provider>
- "show current provider" / "which AI provider are you using" -> category=ai_model ai_action=show_provider
- "show current model" / "which model are you using" -> category=ai_model ai_action=show_model
- "show available providers" / "list AI providers" -> category=ai_model ai_action=show_providers
- "show provider health" / "check AI provider status" -> category=ai_model ai_action=show_health
- "show routing rules" / "what's the routing config" -> category=ai_model ai_action=show_routing
- "queue 10 transactions" / "10 ta transaction koro" / "10 ta tnx koro" / "do 5 payments" / "send 3 \
transactions using my burner wallet" -- a request to START a multi-step batch of transactions/payments, \
with a count but not yet the individual destinations -> category=wallet wallet_action=batch_start \
tx_count=<N> wallet_label=<if a wallet is named, else empty>. Do NOT classify this as category=task -- \
there is no single website/goal yet, only a count; the destinations come in later messages once the \
batch has started.
- "list my wallets" / "show wallets" -> category=wallet wallet_action=list
- "import a wallet" / "add wallet X" (no secret, no method yet) -> category=wallet wallet_action=import \
wallet_label=X
- "import wallet X with my seed phrase" / "add wallet X using a private key" (method named, but the actual \
secret is NOT in this message) -> category=wallet wallet_action=import wallet_label=X \
wallet_import_method=seed_phrase|private_key
- "import wallet X, address 0xabc..." -> category=wallet wallet_action=import wallet_label=X \
wallet_import_method=address wallet_address=0xabc...
- "import wallet X with my private key and set it as hot signer" / "add X, eta diye tnx korte parbe" / \
"make X the hot wallet for auto sends" -> category=wallet wallet_action=import wallet_label=X \
wallet_import_method=private_key|seed_phrase wallet_save_as_hot_signer=true. The secret itself still \
never appears in this message (handled separately) -- only the intent to also save it for direct sending.
- "delete wallet X" / "remove wallet X" -> category=wallet wallet_action=delete wallet_label=X
- "rename wallet X to Y" -> category=wallet wallet_action=rename wallet_label=X wallet_new_name=Y
- "use wallet X" / "switch to wallet X" / "make X active" -> category=wallet wallet_action=select \
wallet_label=X
- "what's the balance of wallet X" / "check X balance" -> category=wallet wallet_action=balance \
wallet_label=X
- "list wallet groups" -> category=wallet wallet_action=groups_list
- "switch wallet X to network Y" -> category=wallet wallet_action=network_switch wallet_label=X \
wallet_network=Y
- "send 0.05 to 0xabc... on base" / "0.1 ETH pathao 0xabc... e ethereum e" / "transfer 2 MATIC to 0xabc... \
on polygon" -> category=wallet wallet_action=send_native send_chain=<chain> send_to_address=0xabc... \
send_amount=<amount>. This is a direct hot-signer native transfer (see backend/wallet/hot_signer.py) -- \
extract chain, address, and amount whenever all three are present, in Bengali/Banglish or English, \
regardless of phrasing (e.g. "koto token pathate hobe", "send", "transfer", "pathao").
- "send 10 of token 0xdef... to 0xabc... on base" / "transfer 50 USDC (contract 0xdef...) to 0xabc... on \
polygon" -> category=wallet wallet_action=send_token send_chain=<chain> send_token_address=0xdef... \
send_to_address=0xabc... send_amount=<amount>. Only classify as send_token when a 0x contract address for \
the token is given in the message -- if the user only names a token by symbol (e.g. "send 10 USDC") with \
no contract address, do NOT set send_token_address to a guess; leave it empty so the app can ask for it.
- "send 0.000001 eth to these 10 addresses: 0xabc..., 0xdef..., ..." / "wallet 1, wallet 2 theke 0xabc... e \
0.01 pathao" / any send/transfer message naming more than one 0x... address, or naming more than one wallet \
label as the sender -- still classify as category=wallet wallet_action=send_native (or send_token, same \
rule) with send_chain and send_amount set as usual. Set send_to_address to just the first address you see; \
the app re-scans the raw message itself for every address/wallet-label and handles 1->many, many->1, and \
many->many sends on its own -- you only need chain/amount right, not the full address list.
- "retry task <id>" / "retry that task" -> category=agent_command action=retry_task task_id=<id if given>
- "delete task <id>" / "remove task <id> from the list" -> category=agent_command action=delete_task \
task_id=<id>
- "list my tasks" / "show all tasks" / "task history" -> category=agent_command action=list_tasks
- "show me the report for task <id>" / "what happened on task <id>" -> category=agent_command \
action=task_report task_id=<id>
- "set task <id> priority to N" / "bump the priority of task <id>" -> category=agent_command \
action=set_priority task_id=<id> priority=<N>
- "change/set/turn on/turn off <setting> (to <value>)" (e.g. "turn off manual approval", "set headless \
to true", "enable vision", "disable ocr") -> category=settings settings_action=update \
settings_field=<field name> settings_value=<value>
- read-only settings/configuration questions ("what model are you using", "what wallet mode") -> \
category=settings settings_action=read
- "create/add a chrome profile named X" -> category=profile profile_action=create profile_label=X
- "list my chrome profiles" / "show profiles" -> category=profile profile_action=list
- "enable profile X" / "disable profile X" -> category=profile profile_action=enable|disable \
profile_label=X
- "delete profile X" -> category=profile profile_action=delete profile_label=X
- "clone profile X as Y" -> category=profile profile_action=clone profile_label=X profile_new_name=Y
- "rename profile X to Y" -> category=profile profile_action=rename profile_label=X profile_new_name=Y
- "open profile X in chrome" / "open X manually" -> category=profile profile_action=open profile_label=X
- "check sessions for profile X" / "is profile X logged in" -> category=profile profile_action=sessions \
profile_label=X
- "use profile X" / "make X the active profile" (about the profile itself, not a task) -> category=profile \
profile_action=select profile_label=X
- "list profile activity" -> category=profile profile_action=activity profile_label=<if named, else empty>
- "search my memory for X" / "what do you remember about X" -> category=memory memory_action=search \
memory_query=X
- "list memories" / "show archived memories" -> category=memory memory_action=list
- "archive/unarchive/forget memory <id>" -> category=memory memory_action=archive|unarchive|forget \
memory_id=<id>
- "find duplicate memories" -> category=memory memory_action=duplicates
- "memory analytics" / "memory stats" -> category=memory memory_action=analytics
- "list plugins" / "show installed plugins" -> category=plugin plugin_action=list
- "enable/disable/reload the X plugin" -> category=plugin plugin_action=enable|disable|reload \
plugin_name=X
- "rescan plugins" / "check for new plugins" -> category=plugin plugin_action=rescan
- "system health" / "run diagnostics" / "check resources" / "what version is this" -> category=system \
system_action=health|diagnostics|resources|version
- "export config" / "backup config" / "list config backups" / "restore backup X" -> category=system \
system_action=config_export|config_backup|config_backups|config_restore \
system_backup_filename=<X if restoring>"""


TX_TARGET_EXTRACTION_PROMPT = """The user is naming ONE destination for a single step of a transaction \
batch they're queuing with an autonomous browser-automation agent (e.g. "0.01 ETH to 0xabc... on \
uniswap.org", "send 5 USDC to bob.eth", "the same site, address 0xdef..."). Extract the website and the \
concrete goal for just this one step. Respond with STRICT JSON only, no prose, no markdown fences:
{"website": "url or domain to act on, else empty", "goal": "one sentence describing exactly what to do \
on that site for this one transaction"}"""


# Wallet-secret guard: matched locally, never sent to any LLM (classifier or
# otherwise). A seed phrase is 12/15/18/21/24 space-separated lowercase
# words; a private key is 64 hex chars, optionally 0x-prefixed. Used both to
# recognize the answer to a pending "paste your secret" prompt (see
# ChatEngine._pending_wallet_import) and, as a standalone safety net, to
# catch a secret pasted with no pending draft at all so it's never
# classified or persisted in the clear either way.
import re as _re

_PRIVATE_KEY_RE = _re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
_SEED_WORD_COUNTS = (12, 15, 18, 21, 24)

# Batch wallet sends (see ChatEngine._handle_send_native / _handle_send_token):
# any 0x + 40 hex chars in the raw message is a candidate destination
# address, matched deterministically -- not left to the LLM classifier,
# since addresses are an unambiguous pattern and money is on the line.
_ETH_ADDRESS_RE = _re.compile(r"0x[0-9a-fA-F]{40}")

# Used only to disambiguate a stale/abandoned task-batch turn (see
# _handle_batch_turn): a plausible website/domain token, so "0.01 ETH to
# 0xabc... on uniswap.org" still reads as a task-batch destination while
# "wallet 1 theke 0x... e transfer koro" (address, no domain) does not.
_DOMAIN_RE = _re.compile(
    r"\b(?:[a-zA-Z0-9-]+\.)+(?:com|org|net|io|xyz|app|finance|eth|co|dev|so|fi)\b"
)


def _extract_addresses(text: str) -> list[str]:
    """All distinct 0x addresses in `text`, in first-seen order."""
    seen: list[str] = []
    for match in _ETH_ADDRESS_RE.findall(text):
        if match not in seen:
            seen.append(match)
    return seen


def _extract_from_wallet_labels(text: str, hot_signers: list[dict]) -> list[str]:
    """
    Which loaded hot-signer addresses are named as SENDERS in `text` (e.g.
    "wallet 1, wallet 2 theke ..." / "from wallet 1 and wallet 2"). Matches
    each hot signer's label (case-insensitive, word-boundary) against the
    raw message; only meaningful with 2+ hits since a single mention is
    already handled by the ordinary single-sender path. Returns addresses
    in the order their labels first appear in the text.
    """
    hits: list[tuple[int, str]] = []
    lowered = text.lower()
    for signer in hot_signers:
        label = (signer.get("label") or "").strip()
        if not label:
            continue
        match = _re.search(r"\b" + _re.escape(label.lower()) + r"\b", lowered)
        if match:
            hits.append((match.start(), signer["address"]))
    hits.sort(key=lambda h: h[0])
    return [addr for _, addr in hits]


def _looks_like_wallet_secret(text: str) -> Optional[str]:
    """Returns 'private_key' | 'seed_phrase' | None."""
    stripped = text.strip()
    if _PRIVATE_KEY_RE.match(stripped):
        return "private_key"
    words = stripped.split()
    if len(words) in _SEED_WORD_COUNTS and all(w.isalpha() and w.islower() for w in words):
        return "seed_phrase"
    return None


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class ChatEngine:
    """
    Stateless besides its DB-backed sessions -- safe to construct once and
    share between the dashboard chat routes and the Telegram bot.
    """

    def __init__(self, queue: Any, app_state: Optional[Any] = None, llm: Optional[LLMClient] = None) -> None:
        self.queue = queue  # TaskQueueService
        self.app_state = app_state  # backend.api.app_state.AppState, optional
        self.llm = llm or _default_model_manager
        # session_id -> {"label": str, "method": "private_key"|"seed_phrase"}.
        # In-memory only, deliberately never persisted -- see
        # _handle_pending_wallet_secret_turn / _looks_like_wallet_secret.
        self._pending_wallet_import: dict[str, dict] = {}

    # ------------------------------------------------------------------ #
    # Session management
    # ------------------------------------------------------------------ #
    async def get_or_create_session(self, session_id: Optional[str], channel: str = "dashboard") -> ChatSession:
        async with get_session() as db:
            if session_id:
                existing = await db.get(ChatSession, session_id)
                if existing:
                    return existing
                row = ChatSession(id=session_id, channel=channel)
            else:
                row = ChatSession(channel=channel)
            db.add(row)
            await db.flush()
            await db.refresh(row)
            return row

    async def list_sessions(self, channel: Optional[str] = None) -> list[ChatSession]:
        async with get_session() as db:
            stmt = select(ChatSession).order_by(ChatSession.updated_at.desc())
            if channel:
                stmt = stmt.where(ChatSession.channel == channel)
            result = await db.execute(stmt)
            return list(result.scalars().all())

    async def get_history(self, session_id: str, limit: int = 200) -> list[ChatMessage]:
        async with get_session() as db:
            result = await db.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.created_at.asc())
                .limit(limit)
            )
            return list(result.scalars().all())

    async def clear_history(self, session_id: str) -> None:
        async with get_session() as db:
            from sqlalchemy import delete

            await db.execute(delete(ChatMessage).where(ChatMessage.session_id == session_id))
            row = await db.get(ChatSession, session_id)
            if row:
                row.last_task_id = None
                row.last_error = None

    # ------------------------------------------------------------------ #
    # Conversation context
    # ------------------------------------------------------------------ #
    # Short-term, per-session conversational context -- distinct from
    # MemoryStore/Chroma (backend/memory/store.py), which only remembers
    # *finished* workflow outcomes/preferences/tool calls across sessions.
    # This is what lets a follow-up like "An AI dashboard" be understood as
    # completing the previous "Build an HTML page" request instead of being
    # classified/answered in isolation. Every LLM call this engine makes
    # (intent classification, plus the free-form conversational reply)
    # should be built from this context, not from the raw message alone.
    CONTEXT_HISTORY_LIMIT = 12

    async def _conversation_context(self, session_id: str, limit: int = CONTEXT_HISTORY_LIMIT) -> str:
        """Recent prior turns for `session_id`, oldest first, formatted as
        'role: content' lines. Excludes the message that triggered the
        current turn (send_message already appended it before this is
        called) -- callers append the new message explicitly via
        _classifier_prompt/_history_prompt. Never raises: a history lookup
        failure degrades to "no context" instead of breaking the turn."""
        try:
            history = await self.get_history(session_id, limit=limit + 1)
        except Exception:
            logger.exception("Failed to load conversation history for context")
            return ""
        if not history:
            return ""
        prior = history[:-1][-limit:]
        lines: list[str] = []
        for m in prior:
            role = m.role.value if hasattr(m.role, "value") else m.role
            content = (m.content or "").strip()
            if not content:
                continue
            if len(content) > 800:
                content = content[:800] + "…"
            lines.append(f"{role}: {content}")
        return "\n".join(lines)

    @staticmethod
    def _history_prompt(context: str, text: str) -> str:
        """Plain transcript shape for free-text LLM calls (e.g. the
        conversational reply) -- reads like a continuing chat log."""
        return f"{context}\nuser: {text}" if context else text

    @staticmethod
    def _classifier_prompt(context: str, text: str) -> str:
        """Labeled shape for the intent classifier: the model must classify
        only the new message, but resolve it against what was just said."""
        if not context:
            return text
        return f"Conversation so far:\n{context}\n\nNew message to classify: {text}"

    # ------------------------------------------------------------------ #
    # Core turn
    # ------------------------------------------------------------------ #
    async def send_message(self, session_id: str, text: str, channel: str = "dashboard") -> dict:
        session = await self.get_or_create_session(session_id, channel)
        await self._append(session.id, ChatRole.USER, text)

        # Teach Mode is intercepted BEFORE intent classification: once a
        # session has an active teach draft (backend.skills.teach.
        # TeachModeManager, keyed by this chat session's id), every message
        # is a teach-mode turn (a step description, or "undo"/"done"/
        # "cancel") until it's finished or cancelled -- it never goes
        # through the LLM classifier, so a step like "type 50 into the
        # amount field" can't accidentally get reclassified as a "task".
        teach = getattr(self.app_state, "teach", None) if self.app_state else None
        if teach is not None and teach.is_active(session.id):
            try:
                reply, meta = await self._handle_teach_turn(session, teach, text)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Teach Mode turn failed")
                reply, meta = f"Something went wrong in Teach Mode: {exc}", {}
            await self._append(session.id, ChatRole.ASSISTANT, reply, category="skill", meta=meta)
            return {"session_id": session.id, "reply": reply, "category": "skill", "action": "teach_step", "meta": meta}

        # Same interception pattern for an active transaction batch
        # (backend.wallet.tx_batch.TxBatchManager): once the user has said
        # e.g. "queue 10 transactions", every following message is treated
        # as one destination for the batch, not reclassified from scratch,
        # so a destination like "0.02 ETH to 0xabc..." can't accidentally
        # be misread as a new unrelated task.
        tx_batch = getattr(self.app_state, "tx_batch", None) if self.app_state else None
        if tx_batch is not None and tx_batch.is_active(session.id):
            try:
                reply, meta = await self._handle_batch_turn(session, tx_batch, text)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Tx batch turn failed")
                reply, meta = f"Something went wrong queuing that: {exc}", {}
            await self._append(session.id, ChatRole.ASSISTANT, reply, category="wallet", meta=meta)
            return {"session_id": session.id, "reply": reply, "category": "wallet", "action": "batch_step", "meta": meta}

        # Pending Chrome Profile selection: intercepted BEFORE intent
        # classification, same pattern as Teach Mode / tx batch above. Every
        # browser task from chat needs a persistent Chrome Profile; once one
        # has been asked for, the next message is read as the answer (a
        # profile name/id, or "cancel") rather than reclassified from scratch.
        pending_profile = getattr(self.app_state, "pending_profile", None) if self.app_state else None
        if pending_profile is not None and pending_profile.is_active(session.id):
            profiles = getattr(self.app_state, "profiles", None) if self.app_state else None
            try:
                reply, meta = await self._handle_pending_profile_turn(session, pending_profile, profiles, text)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Pending profile selection turn failed")
                pending_profile.cancel(session.id)
                reply, meta = f"Something went wrong queuing that: {exc}", {}
            await self._append(session.id, ChatRole.ASSISTANT, reply, category="task", meta=meta)
            return {"session_id": session.id, "reply": reply, "category": "task", "action": "select_profile", "meta": meta}

        # Wallet secret entry: intercepted BEFORE intent classification --
        # this message either answers a pending "paste your seed phrase /
        # private key now" prompt (see _handle_wallet_crud's import
        # branch), or looks like a raw secret with no pending draft at all.
        # Either way it is NEVER passed to the LLM classifier or any other
        # LLM call, and the copy just persisted by _append above is
        # redacted immediately after use so it doesn't sit in chat history
        # in the clear.
        if session.id in self._pending_wallet_import or _looks_like_wallet_secret(text) is not None:
            try:
                reply, meta = await self._handle_pending_wallet_secret_turn(session, text)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Wallet secret import turn failed")
                self._pending_wallet_import.pop(session.id, None)
                reply, meta = f"Something went wrong importing that wallet: {exc}", {}
            await self._redact_last_user_message(session.id)
            await self._append(session.id, ChatRole.ASSISTANT, reply, category="wallet", meta=meta)
            return {"session_id": session.id, "reply": reply, "category": "wallet", "action": "secret_import", "meta": meta}

        context = await self._conversation_context(session.id)
        classifier_input = self._classifier_prompt(context, text)
        try:
            intent = await self.llm.complete_json(
                CLASSIFIER_SYSTEM_PROMPT, classifier_input, task_type=TaskType.FAST_RESPONSE
            )
        except Exception:
            logger.exception("Chat classifier failed, falling back to conversation")
            intent = {"category": "conversation"}

        category = intent.get("category", "conversation")
        action = intent.get("action", "")
        meta: dict[str, Any] = {}

        try:
            if category == "task" and intent.get("website"):
                reply, meta = await self._handle_task(session, intent)
            elif category == "agent_command":
                reply, meta = await self._handle_agent_command(session, action, intent.get("task_id") or "", intent)
            elif category == "browser_command":
                reply, meta = await self._handle_browser_command(session, action, intent)
            elif category == "system_request":
                reply, meta = await self._handle_system_request(session, action)
            elif category == "settings":
                reply, meta = await self._handle_settings(intent)
            elif category == "ai_model":
                reply, meta = await self._handle_ai_model_command(intent)
            elif category == "skill":
                reply, meta = await self._handle_skill_command(session, intent, text)
            elif category == "mcp":
                reply, meta = await self._handle_mcp_command(intent, text)
            elif category == "wallet":
                reply, meta = await self._handle_wallet_command(session, intent, text)
            elif category == "profile":
                reply, meta = await self._handle_profile_command(intent)
            elif category == "memory":
                reply, meta = await self._handle_memory_command(intent)
            elif category == "plugin":
                reply, meta = await self._handle_plugin_command(intent)
            elif category == "system":
                reply, meta = await self._handle_system_command(intent)
            else:
                reply = await self._handle_conversation(session, text)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Chat dispatch failed for category=%s", category)
            reply = f"Something went wrong handling that: {exc}"

        await self._append(session.id, ChatRole.ASSISTANT, reply, category=category, meta=meta)
        return {"session_id": session.id, "reply": reply, "category": category, "action": action, "meta": meta}

    # ------------------------------------------------------------------ #
    # Category handlers
    # ------------------------------------------------------------------ #
    async def _handle_task(self, session: ChatSession, intent: dict) -> tuple[str, dict]:
        website = intent["website"]
        goal = intent.get("goal") or "Complete the available task on this site."
        wallet_label = intent.get("wallet_label") or None
        profile_label = intent.get("profile_label") or None
        return await self._enqueue_with_profile(session, website, goal, wallet_label, profile_label, notes="", priority=1)

    async def _handle_agent_command(
        self, session: ChatSession, action: str, task_id: str = "", intent: Optional[dict] = None
    ) -> tuple[str, dict]:
        intent = intent or {}
        agent = getattr(self.app_state, "agent", None) if self.app_state else None
        if action == "pause":
            if agent:
                await agent.pause()
            else:
                self.queue.pause()
            return "Paused.", {}
        if action == "resume":
            if agent:
                await agent.resume()
            else:
                self.queue.resume()
            return "Resumed.", {}
        if action == "stop":
            if agent:
                await agent.stop()
            else:
                self.queue.pause()
            return "Stopped. In-flight step will finish, then the agent goes idle.", {}
        if action == "start":
            if agent:
                await agent.start()
            return "Started.", {}
        if action == "continue":
            return await self._continue_previous(session)
        # --- Single Task Control: pause/resume/cancel one specific task, ---
        # --- independent of the global worker/agent state above. Reuses  ---
        # --- TaskQueueService.pause_task/resume_task/cancel (backend/    ---
        # --- planner/task_queue.py), the same methods the REST API       ---
        # --- (backend/api/routes_tasks.py) and Dashboard already use, so ---
        # --- Chat/Telegram/Dashboard/REST API all share one source of    ---
        # --- truth for task state.                                      ---
        if action == "pause_task":
            return await self._pause_single_task(task_id)
        if action == "resume_task":
            return await self._resume_single_task(task_id)
        if action == "cancel_task":
            return await self._cancel_single_task(task_id)
        # --- Task management beyond pause/resume/cancel: retry/delete/    ---
        # --- list/history/report/priority. Reuses TaskQueueService and    ---
        # --- the Task/Report tables directly, same source of truth as    ---
        # --- backend/api/routes_tasks.py.                                 ---
        if action == "retry_task":
            return await self._retry_task(task_id)
        if action == "delete_task":
            return await self._delete_task(task_id)
        if action in ("list_tasks", "task_history"):
            return await self._list_tasks()
        if action == "task_report":
            return await self._task_report(task_id)
        if action == "set_priority":
            return await self._set_task_priority(task_id, intent.get("priority"))
        return (
            "Not sure which agent action you mean -- try pause, resume, stop, continue, or name a task "
            "(e.g. 'pause task', 'cancel task <id>', 'retry task <id>', 'list tasks').",
            {},
        )

    async def _retry_task(self, task_id: str) -> tuple[str, dict]:
        target = task_id or None
        if not target:
            return "Which task should I retry? Give me a task id.", {}
        ok = await self.queue.retry(target)
        if not ok:
            return f"Task {target} not found, or not in a retryable (failed/cancelled) state.", {}
        return f"Re-queued task {target} for another attempt.", {"task_id": target}

    async def _delete_task(self, task_id: str) -> tuple[str, dict]:
        target = task_id or None
        if not target:
            return "Which task should I delete? Give me a task id.", {}
        if target in self.queue._task_pause_events or target == self.queue.current_task_id:
            return f"Task {target} is still in flight -- cancel it first, then delete it.", {}
        async with get_session() as db:
            db_task = await db.get(Task, target)
            if db_task is None:
                return f"No task found with id {target}.", {}
            await db.delete(db_task)
        self.queue._cancelled_ids.discard(target)
        return f"Deleted task {target}.", {"task_id": target}

    async def _list_tasks(self) -> tuple[str, dict]:
        async with get_session() as db:
            result = await db.execute(select(Task).order_by(Task.created_at.desc()).limit(10))
            tasks = list(result.scalars().all())
        if not tasks:
            return "No tasks yet.", {}
        lines = ["Recent tasks:"]
        for t in tasks:
            status_val = t.status.value if hasattr(t.status, "value") else t.status
            lines.append(f"- [{status_val}] {t.id} :: {t.website} :: {t.goal[:50]}")
        return "\n".join(lines), {}

    async def _task_report(self, task_id: str) -> tuple[str, dict]:
        if not task_id:
            return "Which task's report do you want? Give me a task id.", {}
        async with get_session() as db:
            result = await db.execute(select(Report).where(Report.task_id == task_id))
            report = result.scalar_one_or_none()
        if report is None:
            return f"No report found for task {task_id} yet.", {}
        return f"Task {task_id} ({report.status}): {report.summary}", {"task_id": task_id}

    async def _set_task_priority(self, task_id: str, priority: Any) -> tuple[str, dict]:
        if not task_id:
            return "Which task's priority should I change? Give me a task id.", {}
        try:
            priority_int = int(priority)
        except (TypeError, ValueError):
            return "What priority value should I set (an integer)?", {}
        async with get_session() as db:
            db_task = await db.get(Task, task_id)
            if db_task is None:
                return f"No task found with id {task_id}.", {}
            db_task.priority = priority_int
        return f"Set task {task_id} priority to {priority_int}.", {"task_id": task_id, "priority": priority_int}

    async def _pause_single_task(self, task_id: str) -> tuple[str, dict]:
        target = task_id or self.queue.current_task_id
        if not target:
            return "No task is currently running to pause.", {}
        ok = self.queue.pause_task(target)
        if not ok:
            return f"Task {target} isn't currently running, so it can't be paused.", {}
        return f"Paused task {target}.", {"task_id": target}

    async def _resume_single_task(self, task_id: str) -> tuple[str, dict]:
        target = task_id
        if not target:
            paused_ids = self.queue.queue_status().get("paused_task_ids") or []
            target = paused_ids[0] if paused_ids else None
        if not target:
            return "No paused task to resume.", {}
        ok = await self.queue.resume_task(target)
        if not ok:
            return f"Task {target} isn't currently paused.", {}
        return f"Resumed task {target}.", {"task_id": target}

    async def _cancel_single_task(self, task_id: str) -> tuple[str, dict]:
        target = task_id or self.queue.current_task_id
        if not target:
            return "No task is currently running to cancel.", {}
        async with get_session() as db:
            db_task = await db.get(Task, target)
        if db_task is None:
            return f"No task found with id {target}.", {}
        if db_task.status in (TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            return f"Task {target} already finished ({db_task.status.value}); nothing to cancel.", {}
        await self.queue.cancel(target)
        return f"Cancelling task {target}.", {"task_id": target}

    async def _continue_previous(self, session: ChatSession) -> tuple[str, dict]:
        # Prefer a task that's actually paused right now.
        qstatus = self.queue.queue_status()
        paused_ids = qstatus.get("paused_task_ids") or []
        if paused_ids:
            task_id = paused_ids[0]
            await self.queue.resume_task(task_id)
            return f"Resuming task {task_id}.", {"task_id": task_id}

        # Otherwise fall back to retrying this session's last known task, if
        # it ended in a retryable (failed/cancelled) state.
        if session.last_task_id:
            ok = await self.queue.retry(session.last_task_id)
            if ok:
                return f"Re-queued task {session.last_task_id} for another attempt.", {"task_id": session.last_task_id}

        agent = getattr(self.app_state, "agent", None) if self.app_state else None
        if agent:
            await agent.resume()
        return "Nothing specific to continue -- resumed the agent worker so it picks up the next queued task.", {}

    async def _handle_browser_command(self, session: ChatSession, action: str, intent: dict) -> tuple[str, dict]:
        live_session = getattr(self.app_state, "live_session", None) if self.app_state else None

        if action == "screenshot":
            if live_session and live_session.latest_screenshot_bytes() is not None:
                return "Here's the latest screenshot (see the Browser panel / GET /api/browser/screenshot).", {
                    "has_screenshot": True
                }
            return "No screenshot available yet -- nothing has run in the browser this session.", {}

        if action == "show":
            if not live_session:
                return "Live browser view isn't initialized in this deployment.", {}
            status = live_session.status()
            if not status.get("active"):
                return "Browser is idle -- no active session right now.", {}
            return f"Browser active: {status.get('title', '')} — {status.get('url', '')}", {}

        if action == "summarize":
            website = self._current_website()
            if not website:
                return "There's no active page to summarize right now -- start a task first.", {}
            profile_label = intent.get("profile_label") or None
            return await self._enqueue_with_profile(
                session, website, "Read the current page and summarize its contents in a few sentences.",
                None, profile_label, notes="", priority=2,
            )

        if action == "search":
            query = intent.get("query") or ""
            if not query:
                return "What would you like me to search for?", {}
            profile_label = intent.get("profile_label") or None
            return await self._enqueue_with_profile(
                session, "https://www.google.com", f"Search for '{query}' and report the top results.",
                None, profile_label, notes="", priority=1,
            )

        if action == "open":
            website = intent.get("website") or intent.get("query") or ""
            if not website:
                return "Which site should I open?", {}
            profile_label = intent.get("profile_label") or None
            return await self._enqueue_with_profile(
                session, website, "Open the page and report what's there.",
                None, profile_label, notes="", priority=1,
            )

        return "Not sure which browser action you mean.", {}

    async def _handle_system_request(self, session: ChatSession, action: str) -> tuple[str, dict]:
        agent = getattr(self.app_state, "agent", None) if self.app_state else None

        if action in ("current_task", "status", ""):
            if not agent:
                return "Agent runtime isn't initialized in this deployment.", {}
            s = await agent.status()
            if not s.get("current_task_id"):
                return "No task currently in flight. Queue worker is " + (
                    "paused." if s.get("queue", {}).get("worker_paused") else "active."
                ), {}
            return (
                f"Working on task {s['current_task_id']} at {s.get('current_website') or '—'}: "
                f"{s.get('current_action') or 'starting'} {s.get('current_target') or ''}".strip()
            ), {}

        if action == "explain_last_action":
            if not agent:
                return "Agent runtime isn't initialized in this deployment.", {}
            s = await agent.status()
            reasoning = s.get("current_reasoning")
            if reasoning:
                return reasoning, {}
            return "No reasoning has been recorded yet -- the agent hasn't taken an action this session.", {}

        if action == "explain_failure":
            report = await self._last_failed_report(session)
            if not report:
                return "I don't see any failed tasks to explain.", {}
            await self._touch_session(session.id, last_error=report.summary)
            return f"Task {report.task_id} ended as {report.status}: {report.summary}", {"task_id": report.task_id}

        if action == "today_summary":
            return await self._today_summary()

        return await self._handle_system_request(session, "current_task")

    # Fields the dashboard's Settings page (backend/api/routes_settings.py)
    # already exposes as safely editable -- same allowlist, so chat can
    # never touch a field the REST API wouldn't also let through (secrets,
    # ports, DB paths stay out of scope either way).
    _SETTINGS_BOOL_FIELDS = {
        "browser_headless", "wallet_require_manual_approval", "vision_enabled",
        "ocr_enabled", "live_session_enabled",
    }
    _SETTINGS_INT_FIELDS = {
        "browser_slow_mo_ms", "browser_default_timeout_ms", "vision_min_elements_threshold",
        "live_session_interval_ms", "live_session_jpeg_quality",
    }
    _SETTINGS_FLOAT_FIELDS = {"wallet_max_auto_approve_value_usd"}
    _SETTINGS_STR_FIELDS = {"llm_model_override", "wallet_allowlisted_contracts", "ocr_lang"}

    async def _handle_settings(self, intent: dict) -> tuple[str, dict]:
        from backend.config.settings import settings
        from backend.planner.model_manager import model_manager

        action = (intent.get("settings_action") or "read").strip().lower()
        if action != "update":
            return (
                f"provider={settings.llm_provider.value} model={settings.llm_model_override or '(default)'} "
                f"routing={model_manager.routing_mode} "
                f"browser={settings.browser_channel.value} headless={settings.browser_headless} "
                f"wallet_manual_approval={settings.wallet_require_manual_approval}",
                {},
            )

        field = (intent.get("settings_field") or "").strip()
        raw_value = intent.get("settings_value")
        if not field:
            return "Which setting would you like to change?", {}

        allowed = (
            self._SETTINGS_BOOL_FIELDS | self._SETTINGS_INT_FIELDS
            | self._SETTINGS_FLOAT_FIELDS | self._SETTINGS_STR_FIELDS
        )
        if field not in allowed:
            return f"'{field}' isn't a setting I can change from chat -- try the Settings page for that.", {}
        if raw_value is None or raw_value == "":
            return f"What should I set {field} to?", {}

        try:
            if field in self._SETTINGS_BOOL_FIELDS:
                value: Any = str(raw_value).strip().lower() in ("true", "1", "yes", "on", "enable", "enabled")
            elif field in self._SETTINGS_INT_FIELDS:
                value = int(raw_value)
            elif field in self._SETTINGS_FLOAT_FIELDS:
                value = float(raw_value)
            else:
                value = str(raw_value)
        except (TypeError, ValueError):
            return f"'{raw_value}' isn't a valid value for {field}.", {}

        setattr(settings, field, value)

        from backend.api.routes_settings import _persist_to_env_file

        _persist_to_env_file({field: value})
        return f"Updated {field} = {value}.", {"field": field, "value": value}

    # ------------------------------------------------------------------ #
    # AI Model Manager
    # ------------------------------------------------------------------ #
    async def _handle_ai_model_command(self, intent: dict) -> tuple[str, dict]:
        from backend.planner.model_manager import TaskType, model_manager, parse_provider_name, parse_task_type

        action = intent.get("ai_action", "")
        provider = parse_provider_name(intent.get("ai_provider", "") or "") if intent.get("ai_provider") else None

        if action == "switch":
            if not provider:
                return "Which provider would you like to switch to? (Claude, GPT, Gemini, Groq, OpenRouter...)", {}
            model_manager.switch_provider(provider)
            return f"Switched to {provider.value}.", {"provider": provider.value}

        if action == "set_default":
            if not provider:
                return "Which provider should be the default?", {}
            model_manager.set_default_provider(provider)
            return f"{provider.value} is now the default provider.", {"provider": provider.value}

        if action == "enable_auto_routing":
            model_manager.enable_auto_routing(True)
            return "Automatic smart routing is now on -- tasks will be routed per the configured rules.", {}

        if action == "disable_auto_routing":
            model_manager.enable_auto_routing(False)
            return f"Automatic routing is off -- using the manually selected provider ({model_manager.current_provider.value}).", {}

        if action == "set_routing_rule":
            if not provider:
                return "Which provider should handle that task type?", {}
            task_type = parse_task_type(intent.get("ai_task_type", "") or "")
            if task_type is None:
                return "Which task type is this rule for (coding, browser automation, vision, fast response, etc.)?", {}
            model_manager.set_routing_rule(task_type, provider)
            return f"Routing rule updated: {task_type.value} -> {provider.value}.", {
                "task_type": task_type.value,
                "provider": provider.value,
            }

        if action == "temporary_use":
            if not provider:
                return "Which provider should I use for this one task?", {}
            model_manager.use_temporarily(provider, reason="chat: one-off override")
            return f"Using {provider.value} for the next request only, then reverting to the normal routing.", {
                "provider": provider.value
            }

        if action == "show_provider":
            return f"Current provider: {model_manager.current_provider.value}", {}

        if action == "show_model":
            return f"Current model: {model_manager.current_model}", {}

        if action == "show_providers":
            names = ", ".join(p.value for p in model_manager.health.keys())
            return f"Available providers: {names}", {}

        if action == "show_health":
            snapshot = model_manager.health_snapshot()
            lines = [
                f"{name}: {info['status']} (avail={info['availability']*100:.0f}%, latency={info['latency_ms'] or '—'}ms)"
                for name, info in snapshot.items()
                if info["total_requests"] > 0
            ]
            if not lines:
                return "No provider health data yet -- nothing has been called or tested this run.", {}
            return "Provider health:\n" + "\n".join(lines), {}

        if action == "show_routing":
            mode = model_manager.routing_mode
            rules = ", ".join(f"{t.value}->{p.value}" for t, p in model_manager.routing_rules.items())
            return f"Routing mode: {mode}. Rules: {rules}", {}

        return "Not sure which AI model action you mean -- try switch/set default/enable auto routing/show provider.", {}

    # ------------------------------------------------------------------ #
    # Wallet: multi-turn transaction batches
    # ------------------------------------------------------------------ #
    # Queues N tasks against a wallet from chat, one destination per turn.
    # Deliberately does NOT touch wallet approval policy -- see
    # backend/wallet/tx_batch.py's module docstring for why that stays
    # Settings-only. Whether each queued task still needs a human click at
    # the wallet-extension popup is unchanged by any of this.
    async def _handle_wallet_command(self, session: ChatSession, intent: dict, text: str = "") -> tuple[str, dict]:
        action = (intent.get("wallet_action") or intent.get("action") or "").strip().lower()

        # --- Wallet CRUD (registry-backed, not batch-related) -- reuses ---
        # --- backend.wallet.registry.WalletRegistry, the same object    ---
        # --- backend/api/routes_wallet.py uses, so chat/dashboard share ---
        # --- one source of truth.                                      ---
        if action in ("list", "import", "delete", "rename", "select", "balance", "groups_list", "network_switch"):
            return await self._handle_wallet_crud(session, action, intent)

        if action == "send_native":
            return await self._handle_send_native(intent, text)

        if action == "send_token":
            return await self._handle_send_token(intent, text)

        tx_batch = getattr(self.app_state, "tx_batch", None) if self.app_state else None
        if tx_batch is None:
            return "Transaction batching isn't enabled in this deployment.", {}

        if action != "batch_start":
            return (
                "Not sure which wallet action you mean -- try \"queue 10 transactions\" to start a batch, "
                "or \"list my wallets\".",
                {},
            )

        try:
            count = int(intent.get("tx_count") or 0)
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            return "How many transactions should I queue up?", {}

        wallet_label = (intent.get("wallet_label") or "").strip() or None
        tx_batch.start(session.id, count, wallet_label)
        wallet_note = f" using {wallet_label}" if wallet_label else ""
        return (
            f"Queuing {count} transaction(s){wallet_note}. Tell me where each one goes, one at a time "
            "(site/address and what to do) -- I'll queue them as you go. Say \"cancel\" to stop early. "
            "Each still goes through your configured wallet-approval policy -- I'm only queuing them, "
            "not changing how they get approved.",
            {"tx_count": count, "wallet_label": wallet_label},
        )

    def _resolve_batch_endpoints(self, text: str, intent_to_address: str) -> tuple[list[str], list[str]]:
        """
        Figure out the from-address(es) and to-address(es) for a send,
        deterministically from the raw message (not the LLM classifier --
        addresses are an unambiguous pattern):
          - to-addresses: every distinct 0x... address in the raw text; if
            none appear verbatim (e.g. carried over from context), falls
            back to the single address the intent classifier extracted.
          - from-addresses: loaded hot-signer wallets whose label is named
            in the text (e.g. "wallet 1, wallet 2 theke ..."). Falls back
            to the current default hot signer when no label is named -- a
            plain single-sender send, same as before batch support existed.
        Both lists come back de-duplicated and in the order they're
        mentioned; a caller with <=1 of each just does an ordinary single
        send, so this is safe to call unconditionally.
        """
        to_addresses = _extract_addresses(text)
        if not to_addresses and intent_to_address:
            to_addresses = [intent_to_address]

        hot_signers = list_hot_signers()
        from_addresses = _extract_from_wallet_labels(text, hot_signers)
        if not from_addresses:
            default_addr = get_hot_signer_address()
            from_addresses = [default_addr] if default_addr else []

        # A to-address that's also one of our own sender wallets isn't a
        # real recipient (e.g. its address happened to get echoed back in
        # the message) -- drop it so it doesn't turn an intended 1->N send
        # into an accidental N<->N.
        from_set = {a.lower() for a in from_addresses}
        to_addresses = [a for a in to_addresses if a.lower() not in from_set]

        return from_addresses, to_addresses

    @staticmethod
    def _short_addr(address: str) -> str:
        return address if len(address) <= 12 else f"{address[:6]}…{address[-4:]}"

    def _format_batch_reply(self, result: BatchTransferResult) -> tuple[str, dict]:
        lines = []
        for i, leg in enumerate(result.legs, 1):
            frm, to = self._short_addr(leg.from_address), self._short_addr(leg.to_address)
            if leg.ok:
                lines.append(f"{i}. {frm} -> {to}: sent, tx {leg.tx_hash}")
            else:
                lines.append(f"{i}. {frm} -> {to}: FAILED -- {leg.error}")

        max_shown = 15
        shown = lines[:max_shown]
        if len(lines) > max_shown:
            shown.append(f"... and {len(lines) - max_shown} more")

        header = f"Batch send on {result.chain}: {result.succeeded} succeeded, {result.failed} failed."
        reply = header + "\n" + "\n".join(shown)
        meta = {
            "chain": result.chain,
            "succeeded": result.succeeded,
            "failed": result.failed,
            "legs": [
                {"from": leg.from_address, "to": leg.to_address, "ok": leg.ok, "tx_hash": leg.tx_hash, "error": leg.error}
                for leg in result.legs
            ],
        }
        return reply, meta

    async def _handle_send_native(self, intent: dict, text: str = "") -> tuple[str, dict]:
        """
        Direct RPC native-token transfer via backend.wallet.hot_signer.HotSigner
        -- a deliberately separate path from the browser-extension approval
        flow the rest of this file uses (see hot_signer.py's module
        docstring). No popup, no human-in-the-loop; only the hot signer's
        own enable flag / per-tx cap gate it.

        Supports 1->1 (ordinary), 1->many, many->1, and many->many (paired)
        sends -- see _resolve_batch_endpoints / hot_signer._pair_addresses.
        """
        hot_signer = getattr(self.app_state, "hot_signer", None) if self.app_state else None
        if hot_signer is None:
            return "Hot signer isn't wired up in this deployment.", {}

        chain = (intent.get("send_chain") or "").strip().lower()
        amount_raw = (intent.get("send_amount") or "").strip()
        from_addresses, to_addresses = self._resolve_batch_endpoints(text, (intent.get("send_to_address") or "").strip())

        if not chain or not to_addresses or not amount_raw:
            missing = []
            if not chain:
                missing.append("chain")
            if not to_addresses:
                missing.append("destination address")
            if not amount_raw:
                missing.append("amount")
            return f"I need the {', '.join(missing)} to send this -- give me all three (e.g. \"send 0.05 to 0xabc... on base\").", {}

        try:
            amount = float(amount_raw)
        except ValueError:
            return f"'{amount_raw}' isn't a valid amount.", {}

        if len(from_addresses) <= 1 and len(to_addresses) <= 1:
            to_address = to_addresses[0]
            from_address = from_addresses[0] if from_addresses else None
            try:
                result = await hot_signer.send_native(chain, to_address, amount, from_address=from_address)
            except HotSignerDisabled as exc:
                return str(exc), {}
            except HotSignerError as exc:
                return f"Send failed: {exc}", {}
            return (
                f"Sent {result.amount_native} native token on {result.chain} to {result.to_address}. "
                f"tx: {result.tx_hash}",
                {"tx_hash": result.tx_hash, "chain": result.chain, "to": result.to_address, "amount": result.amount_native},
            )

        try:
            batch_result = await hot_signer.send_native_batch(chain, from_addresses, to_addresses, amount)
        except HotSignerDisabled as exc:
            return str(exc), {}
        except HotSignerError as exc:
            return f"Batch send failed: {exc}", {}
        return self._format_batch_reply(batch_result)

    async def _handle_send_token(self, intent: dict, text: str = "") -> tuple[str, dict]:
        """
        Direct RPC ERC20 transfer via backend.wallet.hot_signer.HotSigner --
        same no-approval-popup path as _handle_send_native, just calling
        transfer(address,uint256) on the token contract. Same 1->1/1->many/
        many->1/many->many support as _handle_send_native.
        """
        hot_signer = getattr(self.app_state, "hot_signer", None) if self.app_state else None
        if hot_signer is None:
            return "Hot signer isn't wired up in this deployment.", {}

        chain = (intent.get("send_chain") or "").strip().lower()
        token_address = (intent.get("send_token_address") or "").strip()
        amount_raw = (intent.get("send_amount") or "").strip()
        from_addresses, to_addresses = self._resolve_batch_endpoints(text, (intent.get("send_to_address") or "").strip())
        # The token contract address is also a 0x... match -- it's never a
        # recipient, so make sure it didn't leak into to_addresses.
        to_addresses = [a for a in to_addresses if a.lower() != token_address.lower()]

        if not chain or not token_address or not to_addresses or not amount_raw:
            missing = []
            if not chain:
                missing.append("chain")
            if not token_address:
                missing.append("token contract address")
            if not to_addresses:
                missing.append("destination address")
            if not amount_raw:
                missing.append("amount")
            return (
                f"I need the {', '.join(missing)} to send this token -- give me all of it "
                "(e.g. \"send 10 of token 0xtoken... to 0xabc... on base\"). I need the token's "
                "contract address, not just its symbol -- I won't guess one.",
                {},
            )

        try:
            amount = float(amount_raw)
        except ValueError:
            return f"'{amount_raw}' isn't a valid amount.", {}

        if len(from_addresses) <= 1 and len(to_addresses) <= 1:
            to_address = to_addresses[0]
            from_address = from_addresses[0] if from_addresses else None
            try:
                result = await hot_signer.send_token(chain, token_address, to_address, amount, from_address=from_address)
            except HotSignerDisabled as exc:
                return str(exc), {}
            except HotSignerError as exc:
                return f"Send failed: {exc}", {}
            return (
                f"Sent {result.amount_tokens} of token {result.token_address} on {result.chain} to "
                f"{result.to_address}. tx: {result.tx_hash}",
                {
                    "tx_hash": result.tx_hash,
                    "chain": result.chain,
                    "token_address": result.token_address,
                    "to": result.to_address,
                    "amount": result.amount_tokens,
                },
            )

        try:
            batch_result = await hot_signer.send_token_batch(chain, token_address, from_addresses, to_addresses, amount)
        except HotSignerDisabled as exc:
            return str(exc), {}
        except HotSignerError as exc:
            return f"Batch send failed: {exc}", {}
        return self._format_batch_reply(batch_result)

    async def _handle_batch_turn(self, session: ChatSession, tx_batch: Any, text: str) -> tuple[str, dict]:
        """One turn of an active transaction batch (backend.wallet.
        tx_batch.TxBatchManager). "cancel" (and close synonyms) stops the
        batch deterministically without going through the LLM; anything
        else is parsed as one destination via TX_TARGET_EXTRACTION_PROMPT
        and queued as a normal task."""
        lowered = text.strip().lower().rstrip(".!")
        if lowered in ("cancel", "cancel batch", "stop", "stop batch", "abort", "never mind"):
            tx_batch.cancel(session.id)
            return "Cancelled -- nothing further will be queued for this batch.", {}

        draft = tx_batch.get_draft(session.id)
        if draft is None:
            return "No active transaction batch -- say \"queue N transactions\" to start one.", {}

        extraction = await self.llm.complete_json(TX_TARGET_EXTRACTION_PROMPT, text, task_type=TaskType.FAST_RESPONSE)
        website = (extraction.get("website") or "").strip()
        goal = (extraction.get("goal") or text).strip()
        if not website:
            # A stale/abandoned task-batch draft (started earlier with
            # "queue N transactions" and never finished or cancelled) would
            # otherwise trap every later message here forever, asking for a
            # website even when the message is clearly an unrelated direct
            # wallet send (e.g. "wallet 1 theke ... transfer koro 0x...").
            # Detect that case deterministically -- an 0x address with no
            # recognizable website/domain token alongside it -- and drop
            # the stale batch instead of blocking the user on it.
            has_address = bool(_ETH_ADDRESS_RE.search(text))
            has_domain = bool(_DOMAIN_RE.search(text))
            if has_address and not has_domain:
                tx_batch.cancel(session.id)
                return (
                    "That doesn't look like a destination for the pending task batch "
                    "(no website in it), so I've cleared that stale batch. Please resend "
                    "your wallet transfer message.",
                    {},
                )
            return "Which site or address should this one go to?", {}

        task_id = await self.queue.enqueue(website, goal, draft.wallet_label, notes="", priority=1)
        await self._touch_session(session.id, last_task_id=task_id)
        updated = tx_batch.record_queued(session.id, task_id)
        done = len(updated.queued) if updated else 1
        total = updated.total if updated else draft.total
        remaining = updated.remaining if updated else 0

        if remaining > 0:
            return (
                f"Queued {done}/{total}: {goal} on {website} (task_id={task_id}). "
                f"{remaining} more to go -- where's next?",
                {"task_id": task_id},
            )
        return (
            f"Queued {done}/{total}: {goal} on {website} (task_id={task_id}). "
            f"That's all {total} -- batch complete.",
            {"task_id": task_id},
        )

    async def _resolve_wallet(self, wallets: Any, label: str) -> Optional[dict]:
        if not label:
            return None
        found = await wallets.list_wallets(search=label)
        if not found:
            return None
        for w in found:
            if w.get("label", "").strip().lower() == label.strip().lower():
                return w
        return found[0]

    async def _handle_wallet_crud(self, session: ChatSession, action: str, intent: dict) -> tuple[str, dict]:
        wallets = getattr(self.app_state, "wallet_registry", None) if self.app_state else None
        if wallets is None:
            return "The Wallet Registry isn't enabled in this deployment.", {}

        label = (intent.get("wallet_label") or "").strip()

        if action == "list":
            rows = await wallets.list_wallets()
            if not rows:
                return "You don't have any wallets registered yet.", {}
            lines = [f"- {w['label']} ({w.get('address') or 'no address'}) [{w.get('status')}]" for w in rows]
            return "Wallets:\n" + "\n".join(lines), {}

        if action == "groups_list":
            groups = await wallets.list_groups()
            if not groups:
                return "No wallet groups yet.", {}
            return "Wallet groups: " + ", ".join(g["name"] for g in groups), {}

        if action == "import":
            if not label:
                return "What should the new wallet be labeled?", {}

            method = (intent.get("wallet_import_method") or "address").strip().lower()

            if method in ("private_key", "seed_phrase"):
                # Never accept the secret itself in this turn -- start a
                # pending draft and wait for it in the NEXT message, which
                # is intercepted before classification (see
                # send_message / _looks_like_wallet_secret) so the secret
                # never reaches the LLM classifier or any other LLM call.
                raw_flag = str(intent.get("wallet_save_as_hot_signer") or "").strip().lower()
                if raw_flag in ("true", "1", "yes"):
                    save_as_hot_signer = True
                elif raw_flag in ("false", "0", "no"):
                    save_as_hot_signer = False
                else:
                    # Not explicitly mentioned this turn -- fall back to the
                    # server-wide default instead of assuming False.
                    save_as_hot_signer = settings.hot_signer_auto_save_on_import
                self._pending_wallet_import[session.id] = {
                    "label": label,
                    "method": method,
                    "save_as_hot_signer": save_as_hot_signer,
                }
                secret_kind = "seed phrase" if method == "seed_phrase" else "private key"
                hot_signer_note = (
                    " I'll also save it as your hot signer so I can send from it directly, with no "
                    "approval popup -- only do this for a burner/bot wallet."
                    if save_as_hot_signer
                    else ""
                )
                return (
                    f"Ready to import '{label}'. Paste just your {secret_kind} as your next message -- "
                    "nothing else in that message, please. I'll use it only to derive the wallet address "
                    "(it's never stored or logged), and I'll scrub it out of the chat history right after."
                    f"{hot_signer_note} "
                    "Say \"cancel\" instead if you'd rather not.",
                    {"pending": "wallet_secret", "method": method, "save_as_hot_signer": save_as_hot_signer},
                )

            from backend.wallet.import_utils import WalletImportError

            address = (intent.get("wallet_address") or intent.get("query") or "").strip() or None
            try:
                result = await wallets.import_wallet(label=label, method="address", address=address)
            except WalletImportError as exc:
                return f"Couldn't import that wallet: {exc}", {}
            return f"Imported wallet '{label}'.", {"wallet_id": result.get("id")}

        # Everything past here needs an existing wallet resolved by label.
        target = await self._resolve_wallet(wallets, label)
        if target is None:
            rows = await wallets.list_wallets()
            names = ", ".join(w["label"] for w in rows) if rows else "(none yet)"
            return f"I don't have a wallet matching '{label}'. Wallets I know: {names}", {}

        from backend.wallet.registry import WalletNotFoundError

        try:
            if action == "delete":
                await wallets.remove_wallet(target["id"])
                return f"Deleted wallet '{target['label']}'.", {"wallet_id": target["id"]}

            if action == "rename":
                new_name = (intent.get("wallet_new_name") or "").strip()
                if not new_name:
                    return f"What should I rename '{target['label']}' to?", {}
                await wallets.update_wallet(target["id"], label=new_name)
                return f"Renamed wallet '{target['label']}' to '{new_name}'.", {"wallet_id": target["id"]}

            if action == "select":
                await wallets.select_active_wallet(target["id"])
                return f"'{target['label']}' is now the active wallet.", {"wallet_id": target["id"]}

            if action == "balance":
                network = intent.get("wallet_network") or target.get("network")
                if not target.get("address"):
                    return f"Wallet '{target['label']}' has no address on file to check.", {}
                if not network:
                    return f"Which network should I check the balance on for '{target['label']}'?", {}
                if network == "all_evm":
                    return (
                        f"'{target['label']}' is tagged for all EVM chains, not one -- which chain should "
                        "I check (e.g. base, ethereum, polygon)?",
                        {},
                    )
                try:
                    balance = await wallets.get_balance(target["address"], network)
                except (ValueError, RuntimeError) as exc:
                    return f"Couldn't fetch balance: {exc}", {}
                return f"{target['label']} balance on {network}: {balance}", {"wallet_id": target["id"]}

            if action == "network_switch":
                network = (intent.get("wallet_network") or "").strip()
                if not network:
                    return "Which network should I switch to?", {}
                engine = self.queue.current_engine if self.queue else None
                if engine is None:
                    return "No active browser session to switch network on -- run a task first.", {}
                result = await wallets.switch_network(engine, target["id"], network)
                if not result.get("ok"):
                    return f"Network switch failed: {result.get('error', 'unknown error')}", {}
                return f"Switched '{target['label']}' to {network}.", {"wallet_id": target["id"]}
        except WalletNotFoundError:
            return f"Wallet '{target['label']}' no longer exists.", {}

        return "Not sure which wallet action you mean.", {}

    async def _handle_pending_wallet_secret_turn(self, session: ChatSession, text: str) -> tuple[str, dict]:
        """One turn answering a pending "paste your seed phrase / private
        key now" prompt (see _handle_wallet_crud's import branch), or a
        secret pasted with no pending draft at all. Never touches self.llm
        -- the whole point of this path is that the secret is never sent
        to any LLM. import_wallet() only ever derives the checksum address
        from it (backend/wallet/import_utils.py) and never persists the
        secret itself; the caller (send_message) redacts this turn's
        stored chat message right after this returns either way."""
        stripped = text.strip()
        lowered = stripped.lower().rstrip(".!")
        if lowered in ("cancel", "never mind", "nevermind", "stop", "skip"):
            self._pending_wallet_import.pop(session.id, None)
            return "Cancelled -- nothing was imported.", {}

        draft = self._pending_wallet_import.pop(session.id, None)
        if draft is None:
            # No pending import was in flight -- this looked like a secret
            # on its own (_looks_like_wallet_secret), but with no label/
            # method to import it under. Refuse rather than guess.
            return (
                "That looks like a wallet secret, so I'm not going to do anything with it as a standalone "
                "message -- say \"import wallet <label> with a seed phrase\" first so I know what to label "
                "it, then paste the secret when I ask for it.",
                {},
            )

        wallets = getattr(self.app_state, "wallet_registry", None) if self.app_state else None
        if wallets is None:
            return "The Wallet Registry isn't enabled in this deployment.", {}

        detected = _looks_like_wallet_secret(stripped)
        if detected is None or detected != draft["method"]:
            expected = "seed phrase" if draft["method"] == "seed_phrase" else "private key"
            self._pending_wallet_import[session.id] = draft
            return f"That doesn't look like a {expected} -- paste just the {expected}, or say \"cancel\".", {}

        from backend.wallet.import_utils import WalletImportError

        kwargs = {"private_key": stripped} if draft["method"] == "private_key" else {"seed_phrase": stripped}
        try:
            result = await wallets.import_wallet(label=draft["label"], method=draft["method"], **kwargs)
        except WalletImportError as exc:
            return f"Couldn't import that wallet: {exc}", {}

        reply = f"Imported wallet '{draft['label']}' (address derived, secret discarded)."
        meta = {"wallet_id": result.get("id")}

        if draft.get("save_as_hot_signer"):
            # Same in-memory secret from this turn, reused before it goes out
            # of scope -- never round-tripped through the LLM or stored
            # anywhere but here and persist_hot_signer_secret's encrypted
            # keystore write (backend/wallet/keystore.py).
            from backend.wallet.hot_signer import HotSignerPersistError, persist_hot_signer_secret

            try:
                hot_signer_address = persist_hot_signer_secret(label=draft["label"], **kwargs)
            except HotSignerPersistError as exc:
                reply += f" Hot signer setup failed: {exc}"
            else:
                try:
                    await wallets.record_activity(
                        result.get("id") or hot_signer_address,
                        "hot_signer_configured",
                        f"Wallet '{draft['label']}' saved as hot signer ({hot_signer_address})",
                        metadata={"address": hot_signer_address},
                    )
                except Exception:
                    pass
                reply += f" It's also set as your hot signer ({hot_signer_address}) -- I can send from it directly now, no approval popup."
                meta["hot_signer_address"] = hot_signer_address

        return reply, meta

    async def _redact_last_user_message(self, session_id: str) -> None:
        """Overwrites this session's most recent USER chat message with a
        placeholder -- called right after a wallet-secret turn so the raw
        seed phrase/private key doesn't sit in ChatMessage.content (and
        therefore in _conversation_context, which later turns and even
        Telegram history could otherwise surface) in the clear."""
        async with get_session() as db:
            result = await db.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id, ChatMessage.role == ChatRole.USER)
                .order_by(ChatMessage.created_at.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
            if row is not None:
                row.content = "[wallet secret -- redacted from chat history]"

    # ------------------------------------------------------------------ #
    # MCP Core
    # ------------------------------------------------------------------ #
    async def _handle_mcp_command(self, intent: dict, raw_text: str) -> tuple[str, dict]:
        """Dispatches category="mcp" messages to backend.mcp.manager.MCPManager
        (state.mcp) via route_and_call, which auto-selects the connector+tool
        from free text (optionally steered by mcp_connector as a hint). Free
        via Telegram too, since NexusTelegramBot._handle_chat_text ultimately
        calls ChatEngine.send_message."""
        mcp = getattr(self.app_state, "mcp", None) if self.app_state else None
        if mcp is None:
            return "MCP Core isn't enabled in this deployment.", {}

        mcp_action = (intent.get("mcp_action") or "call").strip().lower()
        connector_hint = (intent.get("mcp_connector") or "").strip() or None

        # --- Connector management (list/enable/disable/health) -- ---
        # --- distinct from routing free text to a tool call below. ---
        if mcp_action == "list_connectors":
            connectors = mcp.list_connectors()
            if not connectors:
                return "No MCP connectors configured.", {}
            lines = [f"- {c['name']}: {'enabled' if c.get('enabled') else 'disabled'}" for c in connectors]
            return "MCP connectors:\n" + "\n".join(lines), {}

        if mcp_action in ("enable_connector", "disable_connector"):
            if not connector_hint:
                return "Which connector? (filesystem/terminal/browser/github)", {}
            ok = await (mcp.enable(connector_hint) if mcp_action == "enable_connector" else mcp.disable(connector_hint))
            if not ok:
                return f"Couldn't {'enable' if mcp_action == 'enable_connector' else 'disable'} connector '{connector_hint}'.", {}
            return f"Connector '{connector_hint}' is now {'enabled' if mcp_action == 'enable_connector' else 'disabled'}.", {
                "connector": connector_hint
            }

        if mcp_action == "health":
            health = await mcp.health()
            return f"MCP health: {health}", {}

        query = (intent.get("mcp_query") or "").strip() or raw_text

        result = await mcp.route_and_call(query, connector_hint=connector_hint)
        if result is None:
            return (
                "I couldn't figure out which tool that needs -- try naming the connector "
                "(filesystem/terminal/browser/github) or being more specific.",
                {},
            )

        meta = {"connector": result.connector, "tool": result.tool, "ok": result.ok}
        if not result.ok:
            return f"[{result.connector}.{result.tool}] failed: {result.error}", meta

        # When the filesystem connector just wrote a file, surface its path
        # in meta so the delivery layer (Telegram/Dashboard) can offer the
        # actual file back to the user instead of just a text confirmation
        # -- e.g. NexusTelegramBot._handle_chat/_handle_chat_text sends it
        # as a document when meta["file_path"] is present.
        if (
            result.connector == "filesystem"
            and result.tool in ("write_file", "edit_file")
            and isinstance(result.output, dict)
            and result.output.get("path")
        ):
            meta["file_path"] = result.output["path"]

        return f"[{result.connector}.{result.tool}] {result.output}", meta

    # ------------------------------------------------------------------ #
    # Profile management (backend.identity.registry.ProfileRegistry)
    # ------------------------------------------------------------------ #
    async def _handle_profile_command(self, intent: dict) -> tuple[str, dict]:
        registry = getattr(self.app_state, "profile_registry", None) if self.app_state else None
        if registry is None:
            return "The Identity & Profile Manager isn't enabled in this deployment.", {}

        from backend.identity.registry import ProfileError, ProfileNotFoundError

        action = (intent.get("profile_action") or "").strip().lower()
        label = (intent.get("profile_label") or "").strip()

        if action in ("", "list"):
            rows = await registry.list_profiles()
            if not rows:
                return "You don't have any Chrome Profiles yet.", {}
            lines = [f"- {p['name']} [{p.get('status')}]{' (active)' if p.get('is_active') else ''}" for p in rows]
            return "Chrome Profiles:\n" + "\n".join(lines), {}

        if action == "create":
            if not label:
                return "What should the new profile be named?", {}
            try:
                result = await registry.create_profile(name=label)
            except ProfileError as exc:
                return f"Couldn't create that profile: {exc}", {}
            return f"Created Chrome Profile '{label}'.", {"profile_id": result.get("id")}

        async def _hint() -> str:
            available = await registry.list_profiles(enabled_only=True)
            if not available:
                return "You don't have any Chrome Profiles yet -- create one first."
            return "Available profiles: " + ", ".join(p["name"] for p in available)

        if not label:
            return f"Which profile do you mean? {await _hint()}", {}

        resolved = await registry.resolve(label)
        if resolved is None:
            return f"I don't have a Chrome Profile matching '{label}'. {await _hint()}", {}

        try:
            if action == "enable":
                await registry.set_enabled(resolved.id, True)
                return f"Enabled profile '{resolved.name}'.", {"profile_id": resolved.id}
            if action == "disable":
                await registry.set_enabled(resolved.id, False)
                return f"Disabled profile '{resolved.name}'.", {"profile_id": resolved.id}
            if action == "delete":
                await registry.delete_profile(resolved.id)
                return f"Deleted profile '{resolved.name}'.", {"profile_id": resolved.id}
            if action == "clone":
                new_name = (intent.get("profile_new_name") or "").strip()
                if not new_name:
                    return f"What should the clone of '{resolved.name}' be named?", {}
                await registry.clone_profile(resolved.id, new_name)
                return f"Cloned '{resolved.name}' as '{new_name}'.", {"profile_id": resolved.id}
            if action == "rename":
                new_name = (intent.get("profile_new_name") or "").strip()
                if not new_name:
                    return f"What should I rename '{resolved.name}' to?", {}
                await registry.rename_profile(resolved.id, new_name)
                return f"Renamed '{resolved.name}' to '{new_name}'.", {"profile_id": resolved.id}
            if action == "select":
                await registry.select_active_profile(resolved.id)
                return f"'{resolved.name}' is now the active profile.", {"profile_id": resolved.id}
            if action == "open":
                import asyncio

                from backend.browser.manual_session import ManualChromeSessionError, open_profile_in_chrome

                engine = self.queue.get_engine_for_profile(resolved.id) if self.queue else None
                if engine is not None and engine.user_data_dir == resolved.chrome_profile_dir:
                    return f"'{resolved.name}' is currently loaded by a running task -- wait for it to finish first.", {}
                try:
                    await asyncio.to_thread(open_profile_in_chrome, resolved.chrome_profile_dir)
                except ManualChromeSessionError as exc:
                    return f"Couldn't open Chrome for that profile: {exc}", {}
                return f"Opened '{resolved.name}' in Chrome for you to look around.", {"profile_id": resolved.id}
            if action == "sessions":
                engine = self.queue.get_engine_for_profile(resolved.id) if self.queue else None
                if engine is None:
                    return (
                        f"No active browser session for '{resolved.name}' -- run a task with it first to "
                        "check sessions live. Last known status:\n"
                        f"gmail={resolved.gmail_authenticated} x={resolved.x_authenticated} "
                        f"discord={resolved.discord_authenticated}",
                        {"profile_id": resolved.id},
                    )
                manager = getattr(self.app_state, "profiles", None) if self.app_state else None
                if manager is None:
                    return "The Profile Manager isn't enabled in this deployment.", {}
                from backend.identity.manager import LoadedProfile

                loaded = LoadedProfile(
                    id=resolved.id, name=resolved.name, chrome_profile_dir=resolved.chrome_profile_dir,
                    wallet_label=resolved.wallet_label,
                    configured_services={
                        "gmail": resolved.gmail_account, "x": resolved.x_account, "discord": resolved.discord_account,
                    },
                )
                result = await manager.check_sessions(loaded, engine, notify_fn=None)
                return f"Sessions for '{resolved.name}': {result}", {"profile_id": resolved.id}
            if action == "activity":
                rows = await registry.get_activity(profile_id=resolved.id, limit=10)
                if not rows:
                    return f"No activity recorded for '{resolved.name}' yet.", {}
                lines = [f"- {r.get('event_type')}: {r.get('description')}" for r in rows]
                return f"Recent activity for '{resolved.name}':\n" + "\n".join(lines), {}
        except ProfileNotFoundError:
            return f"Profile '{resolved.name}' no longer exists.", {}
        except ProfileError as exc:
            return f"Couldn't do that: {exc}", {}

        return "Not sure which profile action you mean.", {}

    # ------------------------------------------------------------------ #
    # Memory management (backend.memory.store.MemoryStore)
    # ------------------------------------------------------------------ #
    async def _handle_memory_command(self, intent: dict) -> tuple[str, dict]:
        store = getattr(self.app_state, "memory", None) if self.app_state else None
        if store is None:
            return "Memory isn't enabled in this deployment.", {}

        action = (intent.get("memory_action") or "search").strip().lower()
        entry_id = (intent.get("memory_id") or "").strip()
        query = (intent.get("memory_query") or "").strip()

        if action == "search":
            if not query:
                return "What should I search your memory for?", {}
            results = await store.recall_similar_workflows(website="", goal=query, top_k=5)
            if not results:
                return f"Nothing in memory matches '{query}'.", {}
            lines = [f"- {r.get('summary', r)}" for r in results]
            return f"Memory matches for '{query}':\n" + "\n".join(lines[:5]), {}

        if action == "list":
            rows = await store.list_memories(query=query or None, limit=10)
            if not rows:
                return "No memories match.", {}
            lines = [f"- [{m.get('category')}] {m.get('summary', '')[:80]}" for m in rows]
            return "Memories:\n" + "\n".join(lines), {}

        if action == "analytics":
            stats = await store.get_analytics()
            return f"Memory analytics: {stats}", {}

        if action == "duplicates":
            groups = await store.find_duplicate_groups()
            if not groups:
                return "No duplicate memories found.", {}
            return f"Found {len(groups)} duplicate group(s) -- say which ids to merge to clean them up.", {
                "groups": groups[:5]
            }

        if action == "merge_duplicates":
            ids = intent.get("memory_ids") or []
            if len(ids) < 2:
                return "Give me at least 2 memory ids to merge.", {}
            try:
                result = await store.merge_duplicates(ids)
            except ValueError as exc:
                return f"Couldn't merge those: {exc}", {}
            return "Merged those memories.", {"result": result}

        if action in ("archive", "unarchive"):
            if not entry_id:
                return "Which memory (id) should I archive?", {}
            ok = await (store.archive_memory(entry_id) if action == "archive" else store.unarchive_memory(entry_id))
            if not ok:
                return f"No memory found with id {entry_id}.", {}
            return f"{'Archived' if action == 'archive' else 'Unarchived'} memory {entry_id}.", {"memory_id": entry_id}

        if action == "forget":
            if not entry_id:
                return "Which memory (id) should I forget?", {}
            ok = await store.forget_memory(entry_id)
            if not ok:
                return f"No memory found with id {entry_id}.", {}
            return f"Forgot memory {entry_id}.", {"memory_id": entry_id}

        return "Not sure which memory action you mean -- try search, list, archive, forget, or duplicates.", {}

    # ------------------------------------------------------------------ #
    # Plugin management (backend.plugins.registry.PluginRegistry)
    # ------------------------------------------------------------------ #
    async def _handle_plugin_command(self, intent: dict) -> tuple[str, dict]:
        registry = getattr(self.app_state, "plugins", None) if self.app_state else None
        if registry is None:
            return "The Plugin system isn't enabled in this deployment.", {}

        action = (intent.get("plugin_action") or "list").strip().lower()
        name = (intent.get("plugin_name") or "").strip()

        if action == "list":
            rows = registry.list_plugins()
            if not rows:
                return "No plugins installed.", {}
            lines = [f"- {p['name']}: {'enabled' if p.get('enabled') else 'disabled'}" for p in rows]
            return "Plugins:\n" + "\n".join(lines), {}

        if action == "rescan":
            newly = registry.discover()
            return f"Rescanned -- found {len(newly)} new plugin(s).", {"discovered": newly}

        if action in ("enable", "disable", "reload"):
            if not name:
                return f"Which plugin should I {action}?", {}
            fn = {"enable": registry.enable, "disable": registry.disable, "reload": registry.reload}[action]
            ok = await fn(name)
            if not ok:
                return f"Couldn't {action} plugin '{name}' -- check the name.", {}
            return f"Plugin '{name}' {action}d.", {"plugin": name}

        return "Not sure which plugin action you mean -- try list, enable, disable, reload, or rescan.", {}

    # ------------------------------------------------------------------ #
    # System diagnostics & config (backend.monitoring.*, ConfigManager)
    # ------------------------------------------------------------------ #
    async def _handle_system_command(self, intent: dict) -> tuple[str, dict]:
        action = (intent.get("system_action") or "health").strip().lower()

        if action == "health":
            from backend.monitoring.health import HealthMonitor

            report = await HealthMonitor(self.app_state).check_all()
            return f"System health: {report.to_dict()}", {}

        if action == "diagnostics":
            from backend.monitoring.diagnostics import DiagnosticsService

            report = await DiagnosticsService(self.app_state).run()
            return report.to_text(), {}

        if action == "resources":
            from backend.monitoring.resources import ResourceMonitor

            snapshot = await ResourceMonitor(self.app_state).async_snapshot()
            return f"Resources: {snapshot.to_dict()}", {}

        if action == "version":
            from backend.integrations.github_info import get_build_info

            return f"Version: {get_build_info().to_dict()}", {}

        from backend.config.config_manager import ConfigManager

        if action == "config_export":
            return f"Config: {ConfigManager.export_settings()}", {}

        if action == "config_backup":
            path = ConfigManager.backup()
            return f"Backed up config to {path.name}.", {"filename": path.name}

        if action == "config_backups":
            backups = ConfigManager.list_backups()
            if not backups:
                return "No config backups yet.", {}
            return "Config backups: " + ", ".join(backups), {}

        if action == "config_restore":
            filename = (intent.get("system_backup_filename") or "").strip()
            if not filename:
                return "Which backup filename should I restore?", {}
            try:
                applied = ConfigManager.restore(filename)
            except FileNotFoundError:
                return f"No backup found named '{filename}'.", {}
            return f"Restored config from {filename}.", {"applied": applied}

        return "Not sure which system action you mean -- try health, diagnostics, resources, or version.", {}

    # ------------------------------------------------------------------ #
    # Skill Learning System
    # ------------------------------------------------------------------ #
    async def _handle_skill_command(self, session: ChatSession, intent: dict, raw_text: str) -> tuple[str, dict]:
        """Dispatches category="skill" messages to backend.skills.library.
        SkillService (state.skills) and backend.skills.teach.TeachModeManager
        (state.teach). Both are optional -- when skills_enabled=false
        neither is constructed in backend/main.py -- so this degrades to a
        plain explanation rather than an AttributeError."""
        skills = getattr(self.app_state, "skills", None) if self.app_state else None
        teach = getattr(self.app_state, "teach", None) if self.app_state else None
        if skills is None or teach is None:
            return "The Skill Library isn't enabled in this deployment.", {}

        action = (intent.get("skill_action") or intent.get("action") or "").strip().lower()
        skill_name = (intent.get("skill_name") or "").strip()
        skill_text = (intent.get("skill_text") or "").strip()

        if action == "learn":
            return await self._skill_learn_from_text(skills, teach, skill_text or raw_text)

        if action == "confirm":
            skill = await skills.confirm_pending()
            if skill is None:
                return "There's no pending skill suggestion to save.", {}
            return f"Saved '{skill['name']}' as a skill ({len(skill['workflow'])} step(s)).", {"skill_id": skill["id"]}

        if action == "discard":
            ok = skills.discard_pending()
            return ("Discarded -- I won't save that as a skill.", {}) if ok else (
                "There's no pending skill suggestion to discard.", {}
            )

        if action == "teach_start":
            draft = teach.start(session.id, name=skill_name, trigger="", website_hint="")
            name_part = f" for '{skill_name}'" if skill_name else ""
            return (
                f"Teach Mode started{name_part} -- describe one browser action at a time, e.g. "
                "\"click the Connect Wallet button\" or \"type {{amount}} into the amount field\". "
                "Say \"undo\" to remove the last step, \"done\" to save the skill, or \"cancel\" to discard it.",
                {"draft": draft.__dict__},
            )

        if action == "teach_finish":
            return await self._skill_teach_finish(session, skills, teach)

        if action == "teach_cancel":
            ok = teach.cancel(session.id)
            return ("Cancelled -- nothing was saved.", {}) if ok else ("No active Teach Mode session to cancel.", {})

        if action == "teach_undo":
            ok = teach.undo_last_step(session.id)
            return ("Removed the last step.", {}) if ok else ("No active Teach Mode session, or nothing to undo.", {})

        if action == "list":
            return await self._skill_list(skills)

        if action in ("enable", "disable"):
            target = await self._find_skill_by_name(skills, skill_name)
            if target is None:
                return f"Couldn't find a skill matching '{skill_name}'.", {}
            enabled = action == "enable"
            updated = await skills.set_enabled(target["id"], enabled)
            verb = "Enabled" if enabled else "Disabled"
            return f"{verb} '{updated['name']}'.", {"skill_id": updated["id"]}

        if action == "delete":
            target = await self._find_skill_by_name(skills, skill_name)
            if target is None:
                return f"Couldn't find a skill matching '{skill_name}'.", {}
            await skills.delete(target["id"])
            return f"Deleted '{target['name']}'.", {}

        if action == "correct":
            return await self._skill_correct(skills, teach, skill_name, skill_text or raw_text)

        return (
            "Not sure which skill action you mean -- try \"list my skills\", \"teach me a skill\", "
            "or \"learn how to ...\".",
            {},
        )

    async def _skill_learn_from_text(self, skills: Any, teach: Any, text: str) -> tuple[str, dict]:
        if not text:
            return "What should I learn? Describe the steps, e.g. \"learn how to check the gas price on etherscan\".", {}
        draft = await teach.parse_skill_from_text(text)
        if not draft.get("workflow"):
            return (
                "I couldn't pull concrete steps out of that -- try describing the exact clicks/typing "
                "involved, or say \"teach me a skill\" to walk through it step by step instead.",
                {},
            )
        skill = await skills.create(
            name=draft.get("name") or text[:60],
            description=draft.get("description", ""),
            category=draft.get("category", "general"),
            trigger=draft.get("trigger", ""),
            variables=draft.get("variables") or [],
            workflow=draft.get("workflow") or [],
            website_hint=draft.get("website_hint"),
            source=SkillSource.NATURAL_LANGUAGE,
        )
        return f"Learned a new skill: '{skill['name']}' ({len(skill['workflow'])} step(s)).", {"skill_id": skill["id"]}

    async def _skill_teach_finish(self, session: ChatSession, skills: Any, teach: Any) -> tuple[str, dict]:
        draft = teach.get_draft(session.id)
        if draft is None:
            return "No active Teach Mode session -- say \"teach me a skill\" to start one.", {}
        if not draft.steps:
            teach.cancel(session.id)
            return "No steps were taught, so there's nothing to save -- Teach Mode session ended.", {}
        teach.finish(session.id)
        skill = await skills.create(
            name=draft.name or "Taught skill",
            description=draft.description,
            category=draft.category,
            trigger=draft.trigger,
            website_hint=draft.website_hint or None,
            variables=draft.variables,
            workflow=draft.steps,
            source=SkillSource.TEACH_MODE,
        )
        return f"Saved skill '{skill['name']}' with {len(skill['workflow'])} step(s).", {"skill_id": skill["id"]}

    async def _skill_list(self, skills: Any) -> tuple[str, dict]:
        items = await skills.list()
        if not items:
            return "No skills learned yet -- try \"teach me a skill\" or \"learn how to ...\".", {}
        lines = ["Learned skills:"]
        for s in items[:20]:
            flag = "" if s["enabled"] else " (disabled)"
            lines.append(f"- {s['name']}{flag} — used {s['usage_count']}x, {int(s['success_rate'] * 100)}% success")
        if len(items) > 20:
            lines.append(f"...and {len(items) - 20} more. See the Skills page for the full list.")
        return "\n".join(lines), {"count": len(items)}

    async def _skill_correct(self, skills: Any, teach: Any, skill_name: str, instruction: str) -> tuple[str, dict]:
        if not instruction:
            return "What should that step have done instead?", {}
        target = await self._find_skill_by_name(skills, skill_name) or await self._most_recently_used_skill(skills)
        if target is None:
            return "I don't have a skill to correct yet -- mention which skill you mean.", {}
        corrected_step = await teach.parse_correction(instruction)
        workflow = list(target["workflow"])
        if workflow:
            workflow[-1] = corrected_step
            note = "corrected last step via chat"
        else:
            workflow.append(corrected_step)
            note = "appended corrected step via chat"
        updated = await skills.update(target["id"], {"workflow": workflow}, change_note=note)
        if updated is None:
            return f"Couldn't update '{target['name']}'.", {}
        return (
            f"Updated '{updated['name']}' -- {corrected_step.get('description') or 'step corrected'}.",
            {"skill_id": updated["id"]},
        )

    @staticmethod
    async def _find_skill_by_name(skills: Any, name: str) -> Optional[dict[str, Any]]:
        if not name:
            return None
        matches = await skills.list(search=name)
        if not matches:
            return None
        lname = name.strip().lower()
        for s in matches:
            if s["name"].strip().lower() == lname:
                return s
        return matches[0]

    @staticmethod
    async def _most_recently_used_skill(skills: Any) -> Optional[dict[str, Any]]:
        items = await skills.list()
        used = [s for s in items if s.get("last_used_at")]
        if not used:
            return None
        used.sort(key=lambda s: s["last_used_at"], reverse=True)
        return used[0]

    async def _handle_teach_turn(self, session: ChatSession, teach: Any, text: str) -> tuple[str, dict]:
        """One turn of an active Teach Mode session (backend.skills.teach.
        TeachModeManager). "undo"/"done"/"cancel" (and close synonyms) are
        matched directly rather than routed through the LLM classifier, so
        they behave deterministically even if a taught step happens to
        contain a similar word; anything else is parsed as a step via
        teach.add_step_from_text (which itself calls the LLM once, on the
        step text only)."""
        lowered = text.strip().lower().rstrip(".!")

        if lowered in ("cancel", "cancel teaching", "stop teaching", "forget this", "forget it", "abort"):
            teach.cancel(session.id)
            return "Cancelled -- I won't save that skill.", {}

        if lowered in ("undo", "undo that", "undo last step", "undo the last step", "undo step"):
            ok = teach.undo_last_step(session.id)
            return ("Removed the last step.", {}) if ok else ("Nothing to undo yet.", {})

        if lowered in ("done", "finish", "that's it", "thats it", "save it", "finished", "done teaching", "save"):
            skills = getattr(self.app_state, "skills", None) if self.app_state else None
            if skills is None:
                teach.cancel(session.id)
                return "The Skill Library isn't enabled in this deployment -- Teach Mode session ended.", {}
            return await self._skill_teach_finish(session, skills, teach)

        step = await teach.add_step_from_text(session.id, text)
        if step is None:
            return (
                "Couldn't parse that as a step -- try describing one browser action, e.g. "
                "\"click the Submit button\".",
                {},
            )
        draft = teach.get_draft(session.id)
        count = len(draft.steps) if draft else 0
        return (
            f"Got it: {step.get('description') or step.get('action')}. "
            f"({count} step(s) so far — say \"done\" to finish, \"undo\" to remove the last one, or keep going.)",
            {"step": step},
        )

    async def _handle_conversation(self, session: ChatSession, text: str) -> str:
        agent = getattr(self.app_state, "agent", None) if self.app_state else None
        live_session = getattr(self.app_state, "live_session", None) if self.app_state else None

        status_line = "unknown"
        if agent:
            s = await agent.status()
            status_line = (
                f"status={s.get('status')} current_task={s.get('current_task_id') or 'none'} "
                f"current_website={s.get('current_website') or '—'} "
                f"tasks_completed={s.get('tasks_completed', 0)} tasks_failed={s.get('tasks_failed', 0)}"
            )
        browser_line = "unknown"
        if live_session:
            b = live_session.status()
            browser_line = f"active={b.get('active')} url={b.get('url') or '—'}"

        context = await self._conversation_context(session.id)

        system_prompt = (
            "You are Nexus-Agent, chatting naturally with your operator. You control an autonomous "
            "browser-automation agent (Playwright-driven, non-custodial wallet approvals, task queue). "
            "Answer naturally and concisely -- a sentence or two for simple questions, more only if "
            "genuinely needed. If the user asks you to do something actionable, tell them what you're "
            "about to do; otherwise just answer.\n\n"
            f"Current agent status: {status_line}\n"
            f"Current browser: {browser_line}\n"
            f"Last known error this session: {session.last_error or 'none'}"
        )
        user_prompt = self._history_prompt(context, text)
        reply = await self.llm.complete_text(system_prompt, user_prompt, task_type=TaskType.GENERAL_CHAT)
        return reply.strip() or "..."

    # ------------------------------------------------------------------ #
    # Chrome Profile enforcement for browser tasks
    # ------------------------------------------------------------------ #
    # Every browser task queued from chat (category=task, or a browser_command
    # that itself queues one -- open/search/summarize) must run against a
    # named, persistent Chrome Profile (backend/identity/) rather than a
    # throwaway context. This is the single choke point both paths go
    # through: it resolves a named profile, auto-picks the one profile if
    # there's exactly one, asks the user to choose among several (parking
    # the task via PendingProfileManager until they answer), or tells them
    # to create one first if none exist yet.
    async def _enqueue_with_profile(
        self,
        session: ChatSession,
        website: str,
        goal: str,
        wallet_label: Optional[str],
        profile_label: Optional[str],
        notes: str = "",
        priority: int = 1,
    ) -> tuple[str, dict]:
        profiles = getattr(self.app_state, "profiles", None) if self.app_state else None
        pending_profile = getattr(self.app_state, "pending_profile", None) if self.app_state else None

        if profiles is None:
            # Identity & Profile Manager not enabled in this deployment --
            # fully restores prior behavior rather than blocking every task.
            task_id = await self.queue.enqueue(website, goal, wallet_label, notes=notes, priority=priority)
            await self._touch_session(session.id, last_task_id=task_id)
            return f"Queued a task on {website}: {goal}\n(task_id={task_id})", {"task_id": task_id}

        if profile_label:
            resolved = await profiles.registry.resolve(profile_label)
            if resolved is None:
                return (
                    f"I don't have a Chrome Profile named '{profile_label}'. "
                    f"{await self._no_such_profile_hint(profiles)}",
                    {},
                )
            return await self._enqueue_now(session, website, goal, wallet_label, resolved.name, notes, priority)

        available = await profiles.registry.list_profiles(enabled_only=True)
        if not available:
            return (
                "Browser tasks need a Chrome Profile so cookies/login/session state have somewhere "
                "persistent to live, and you don't have one yet. Create one first (Chrome Profiles page, "
                "or tell me a name and I'll set one up), then re-send this task.",
                {},
            )
        if len(available) == 1:
            return await self._enqueue_now(session, website, goal, wallet_label, available[0]["name"], notes, priority)

        # Multi-Profile Browser Management: no need to stop and ask which
        # profile to use when several exist -- auto-pick the best available
        # one (idle over busy/needs-login, matching account for the site's
        # domain preferred, most-recently-used as a tiebreak) and queue
        # immediately. The user can still always override by naming a
        # profile explicitly (handled by the `profile_label` branch above).
        best = ProfileManager.choose_best_profile(available, website=website)
        if best is not None:
            reply, meta = await self._enqueue_now(session, website, goal, wallet_label, best["name"], notes, priority)
            meta = {**meta, "auto_selected_profile": True}
            return (
                f"{reply}\n(Auto-selected Chrome Profile '{best['name']}' out of {len(available)} available -- "
                "say 'use <profile name>' next time to pick a specific one.)",
                meta,
            )

        # Shouldn't normally happen (available is non-empty), but fall back
        # to asking rather than silently dropping the task if it ever does.
        if pending_profile is not None:
            pending_profile.start(session.id, PendingTask(website, goal, wallet_label, notes, priority))
        names = ", ".join(p["name"] for p in available)
        return (
            f"Which Chrome Profile should I use for this task on {website}? Options: {names}. "
            "(Say 'cancel' to skip it.)",
            {},
        )

    async def _enqueue_now(
        self,
        session: ChatSession,
        website: str,
        goal: str,
        wallet_label: Optional[str],
        profile_label: str,
        notes: str,
        priority: int,
    ) -> tuple[str, dict]:
        task_id = await self.queue.enqueue(
            website, goal, wallet_label, notes=notes, priority=priority, profile_label=profile_label
        )
        await self._touch_session(session.id, last_task_id=task_id)
        return (
            f"Queued a task on {website} using Chrome Profile '{profile_label}': {goal}\n(task_id={task_id})",
            {"task_id": task_id, "profile_label": profile_label},
        )

    @staticmethod
    async def _no_such_profile_hint(profiles: Any) -> str:
        available = await profiles.registry.list_profiles(enabled_only=True)
        if not available:
            return "You don't have any Chrome Profiles yet -- create one first."
        names = ", ".join(p["name"] for p in available)
        return f"Available profiles: {names}"

    async def _handle_pending_profile_turn(
        self, session: ChatSession, pending_profile: Any, profiles: Any, text: str
    ) -> tuple[str, dict]:
        lowered = text.strip().lower().rstrip(".!")
        if lowered in ("cancel", "never mind", "nevermind", "stop", "skip"):
            pending_profile.cancel(session.id)
            return "Cancelled -- that task wasn't queued.", {}

        pending = pending_profile.get(session.id)
        if pending is None:
            return "No task is currently waiting on a profile choice.", {}

        if profiles is None:
            pending_profile.cancel(session.id)
            return "The Identity & Profile Manager isn't enabled in this deployment.", {}

        resolved = await profiles.registry.resolve(text.strip())
        if resolved is None:
            hint = await self._no_such_profile_hint(profiles)
            return f"I don't have a Chrome Profile matching '{text.strip()}'. {hint} Or say 'cancel'.", {}

        pending_profile.cancel(session.id)
        return await self._enqueue_now(
            session, pending.website, pending.goal, pending.wallet_label, resolved.name, pending.notes, pending.priority
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _current_website(self) -> Optional[str]:
        live_session = getattr(self.app_state, "live_session", None) if self.app_state else None
        if live_session:
            status = live_session.status()
            if status.get("active"):
                return status.get("url") or None
        return None

    async def _last_failed_report(self, session: ChatSession) -> Optional[Report]:
        async with get_session() as db:
            candidate_id = session.last_task_id
            if candidate_id:
                result = await db.execute(select(Report).where(Report.task_id == candidate_id))
                report = result.scalar_one_or_none()
                if report and report.status in ("failed", "cancelled"):
                    return report
            result = await db.execute(
                select(Report).where(Report.status == "failed").order_by(Report.created_at.desc()).limit(1)
            )
            return result.scalar_one_or_none()

    async def _today_summary(self) -> tuple[str, dict]:
        start = _now().replace(hour=0, minute=0, second=0, microsecond=0)
        async with get_session() as db:
            tasks_result = await db.execute(select(Task).where(Task.created_at >= start))
            tasks = list(tasks_result.scalars().all())
            reports_result = await db.execute(select(Report).where(Report.created_at >= start))
            reports = list(reports_result.scalars().all())

        if not tasks and not reports:
            return "Nothing has run today yet.", {}

        succeeded = sum(1 for r in reports if r.status == "succeeded")
        failed = sum(1 for r in reports if r.status in ("failed", "cancelled"))
        lines = [f"Today: {len(tasks)} task(s) queued, {succeeded} succeeded, {failed} failed/blocked."]
        for t in tasks[:10]:
            status_val = t.status.value if hasattr(t.status, "value") else t.status
            lines.append(f"- [{status_val}] {t.website} :: {t.goal[:60]}")
        return "\n".join(lines), {}

    async def _touch_session(self, session_id: str, last_task_id: Optional[str] = None, last_error: Optional[str] = None) -> None:
        async with get_session() as db:
            row = await db.get(ChatSession, session_id)
            if row is None:
                return
            if last_task_id is not None:
                row.last_task_id = last_task_id
            if last_error is not None:
                row.last_error = last_error

    async def _append(
        self, session_id: str, role: ChatRole, content: str, category: Optional[str] = None, meta: Optional[dict] = None
    ) -> None:
        async with get_session() as db:
            db.add(ChatMessage(session_id=session_id, role=role, content=content, category=category, meta_json=meta or {}))
