"""
SQLAlchemy ORM models for Nexus-Agent's persistent state.
"""
from __future__ import annotations

import datetime as dt
import enum
import uuid
from typing import Optional

from sqlalchemy import (
    JSON,
    Enum as SAEnum,
    ForeignKey,
    String,
    Text,
    Float,
    Boolean,
    DateTime,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class TaskStatus(str, enum.Enum):
    QUEUED = "queued"
    PLANNING = "planning"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    website: Mapped[str] = mapped_column(String(2048))
    goal: Mapped[str] = mapped_column(Text)
    wallet_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Loose reference to ProfileRecord.name (mirrors wallet_label's convention: no FK
    # constraint, resolved by name at run time). None fully restores prior behavior --
    # a plain BrowserEngine() with no persistent Chrome profile / no auto wallet.
    profile_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[int] = mapped_column(default=0)
    status: Mapped[TaskStatus] = mapped_column(SAEnum(TaskStatus), default=TaskStatus.QUEUED)
    scheduled_for: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    retry_count: Mapped[int] = mapped_column(default=0)
    max_retries: Mapped[int] = mapped_column(default=2)

    steps: Mapped[list["TaskStep"]] = relationship(back_populates="task", cascade="all, delete-orphan")
    report: Mapped["Report"] = relationship(back_populates="task", uselist=False, cascade="all, delete-orphan")


class TaskStep(Base):
    __tablename__ = "task_steps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    index: Mapped[int] = mapped_column()
    action: Mapped[str] = mapped_column(String(64))  # e.g. click, type, navigate, wait, verify
    target_description: Mapped[str] = mapped_column(Text)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)  # typed text / navigate URL, if any
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    screenshot_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    task: Mapped[Task] = relationship(back_populates="steps")


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), unique=True)
    status: Mapped[str] = mapped_column(String(32))
    summary: Mapped[str] = mapped_column(Text)
    execution_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    tx_hashes: Mapped[list] = mapped_column(JSON, default=list)
    screenshots: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    task: Mapped[Task] = relationship(back_populates="report")


class WalletStatus(str, enum.Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    LOCKED = "locked"
    UNKNOWN = "unknown"


class WalletGroup(Base):
    """
    A user-defined grouping of wallets (e.g. "testnet", "farming",
    "client-A"). Purely organizational -- carries no credentials.
    """

    __tablename__ = "wallet_groups"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    wallets: Mapped[list["WalletRecord"]] = relationship(back_populates="group")


class WalletRecord(Base):
    """
    Metadata only. Nexus-Agent never stores seed phrases or raw private keys
    in this table, in this database, or anywhere else at rest -- see
    backend/wallet/import_utils.py and backend/wallet/manager.py for the
    key-handling policy. Import flows that accept a seed phrase or private
    key use the secret only in-memory, for the single call that derives the
    checksum address, and then discard it.
    """

    __tablename__ = "wallets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    label: Mapped[str] = mapped_column(String(128), unique=True)
    address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    wallet_type: Mapped[str] = mapped_column(String(32), default="metamask")  # metamask, rabby, walletconnect, browser_profile
    provider: Mapped[str] = mapped_column(String(32), default="metamask")  # kept for backward compat with existing callers
    network: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[WalletStatus] = mapped_column(SAEnum(WalletStatus), default=WalletStatus.UNKNOWN)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    group_id: Mapped[str | None] = mapped_column(ForeignKey("wallet_groups.id"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    group: Mapped[Optional["WalletGroup"]] = relationship(back_populates="wallets")
    activity: Mapped[list["WalletActivity"]] = relationship(back_populates="wallet", cascade="all, delete-orphan")


class WalletActivity(Base):
    """
    Append-only audit log of wallet-related events (imported, selected,
    network switched, popup detected, tx/signature seen, tx status update).
    Never contains secrets -- only labels, addresses, and descriptions.
    """

    __tablename__ = "wallet_activity"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    wallet_id: Mapped[str] = mapped_column(ForeignKey("wallets.id"))
    event_type: Mapped[str] = mapped_column(String(48))
    description: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    wallet: Mapped[WalletRecord] = relationship(back_populates="activity")


class ProfileStatus(str, enum.Enum):
    READY = "ready"  # enabled, sessions checked (or never checked), not currently in a task
    IN_USE = "in_use"  # currently driving a running task
    NEEDS_LOGIN = "needs_login"  # last session check found >=1 configured service not authenticated
    DISABLED = "disabled"  # enabled=False
    ERROR = "error"


class ProfileRecord(Base):
    """
    Metadata for one Browser Profile (Identity & Profile Manager) -- "one
    complete online identity": a name, a Chrome profile directory, and
    which wallet/Gmail/X/Discord accounts belong to it. Exactly like
    WalletRecord above, this table never stores a password, seed phrase,
    or private key. Cookies, local storage, session storage, and installed
    extensions are never duplicated into this database either -- they
    already live on disk inside `chrome_profile_dir`, persisted natively by
    Chrome/Playwright's persistent context (see backend/browser/engine.py
    and backend/identity/fs.py). This table only tracks *metadata about*
    that directory (which identity it belongs to) and the last-known
    authentication status per service, refreshed by
    backend/identity/detector.py.
    """

    __tablename__ = "profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    chrome_profile_dir: Mapped[str] = mapped_column(String(1024))
    wallet_label: Mapped[str | None] = mapped_column(String(128), nullable=True)  # loose reference, mirrors Task.wallet_label
    gmail_account: Mapped[str | None] = mapped_column(String(256), nullable=True)
    x_account: Mapped[str | None] = mapped_column(String(256), nullable=True)
    discord_account: Mapped[str | None] = mapped_column(String(256), nullable=True)
    extensions: Mapped[list] = mapped_column(JSON, default=list)  # known extension ids/names (metadata only)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[ProfileStatus] = mapped_column(SAEnum(ProfileStatus), default=ProfileStatus.READY)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    gmail_authenticated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    x_authenticated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    discord_authenticated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_session_check_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    activity: Mapped[list["ProfileActivity"]] = relationship(back_populates="profile", cascade="all, delete-orphan")


class ProfileActivity(Base):
    """Append-only audit log for a profile (created, cloned, renamed, deleted,
    imported, exported, enabled/disabled, selected, session_checked, task_started).
    Never contains secrets -- only labels and descriptions, same policy as
    WalletActivity above."""

    __tablename__ = "profile_activity"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    profile_id: Mapped[str] = mapped_column(ForeignKey("profiles.id"))
    event_type: Mapped[str] = mapped_column(String(48))
    description: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    profile: Mapped[ProfileRecord] = relationship(back_populates="activity")


class AgentRuntimeStatus(str, enum.Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"


class AgentRuntimeState(Base):
    """
    Single-row (id="singleton") persistent record of the Autonomous Agent
    Runtime's status, so it survives process restarts -- e.g. the dashboard
    can show "running" was the last known state, and startup recovery
    (backend/planner/agent_runtime.py) can tell an unclean shutdown left
    tasks stuck mid-flight and requeue them.
    """

    __tablename__ = "agent_runtime_state"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: "singleton")
    status: Mapped[AgentRuntimeStatus] = mapped_column(SAEnum(AgentRuntimeStatus), default=AgentRuntimeStatus.STOPPED)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stopped_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    current_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    current_website: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    current_action: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_target: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)

    tasks_completed: Mapped[int] = mapped_column(default=0)
    tasks_failed: Mapped[int] = mapped_column(default=0)
    steps_executed: Mapped[int] = mapped_column(default=0)
    recoveries_performed: Mapped[int] = mapped_column(default=0)

    last_heartbeat_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class ChatRole(str, enum.Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ChatSession(Base):
    """
    A conversational session (dashboard "AI Chat" page, or one per Telegram
    chat_id). Holds only small, non-secret continuity state -- the last task
    the session touched and the last error it saw -- so the chat can answer
    "continue the previous task" / "explain why you failed" without needing
    the user to repeat themselves. Everything else "current" (task/browser/
    website/action) is read live from AgentRuntime/LiveSessionManager at
    answer time rather than duplicated here, so it can never go stale.
    """

    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=_uuid)
    channel: Mapped[str] = mapped_column(String(32), default="dashboard")  # dashboard | telegram
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    last_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="ChatMessage.created_at"
    )


class ChatMessage(Base):
    """One turn of a ChatSession. `category` is the classifier's label
    (conversation/question/browser_command/agent_command/task/settings/
    system_request) for user turns; meta_json carries any side effect
    (e.g. {"task_id": "..."} when a message created a task)."""

    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.id"))
    role: Mapped[ChatRole] = mapped_column(SAEnum(ChatRole))
    content: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    meta_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    session: Mapped[ChatSession] = relationship(back_populates="messages")


class SkillSource(str, enum.Enum):
    """How a Skill first came into existence -- surfaced in the UI/API so a
    user can tell an agent-authored skill (from a successful task outcome)
    apart from one they explicitly taught or imported."""

    NATURAL_LANGUAGE = "natural_language"
    TEACH_MODE = "teach_mode"
    BROWSER_DEMONSTRATION = "browser_demonstration"
    RECORDED_WORKFLOW = "recorded_workflow"
    TASK_OUTCOME = "task_outcome"
    CORRECTION = "correction"
    IMPORTED = "imported"
    MANUAL = "manual"


class Skill(Base):
    """
    A reusable, replayable unit of learned behavior. `workflow` is an
    ordered list of step dicts (same shape as backend.planner.agent_loop
    StepResult inputs: action/target/value/description), each of which may
    reference `{{variable_name}}` placeholders resolved at run time from
    `variables` + caller-supplied overrides. See backend/skills/ for the
    library/matcher/runner/teach modules that own this table.
    """

    __tablename__ = "skills"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(64), default="general")
    # Newline-separated natural-language trigger phrases, e.g.
    # "check the gas price\nwhat's gwei right now". The matcher does both a
    # fast substring pass and a semantic (embedding) pass over this field.
    trigger: Mapped[str] = mapped_column(Text, default="")
    variables: Mapped[list] = mapped_column(JSON, default=list)  # [{"name","description","default"}]
    workflow: Mapped[list] = mapped_column(JSON, default=list)  # [{"action","target","value","description"}]
    success_condition: Mapped[str | None] = mapped_column(Text, nullable=True)
    required_plugins: Mapped[list] = mapped_column(JSON, default=list)
    required_browser: Mapped[str | None] = mapped_column(String(32), nullable=True)
    website_hint: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    success_rate: Mapped[float] = mapped_column(Float, default=0.0)
    usage_count: Mapped[int] = mapped_column(default=0)
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[SkillSource] = mapped_column(SAEnum(SkillSource), default=SkillSource.MANUAL)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    versions: Mapped[list["SkillVersion"]] = relationship(
        back_populates="skill", cascade="all, delete-orphan", order_by="SkillVersion.version"
    )


class SkillVersion(Base):
    """Immutable snapshot of a Skill taken every time it's edited, so Edit /
    a bad correction / a bad Teach-Mode session can be rolled back."""

    __tablename__ = "skill_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"))
    version: Mapped[int] = mapped_column()
    snapshot_json: Mapped[dict] = mapped_column(JSON, default=dict)
    change_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    skill: Mapped[Skill] = relationship(back_populates="versions")


class MemoryEntry(Base):
    """
    Structured record mirroring what is embedded into ChromaDB, kept here too
    so the SQL side can be queried/filtered without touching the vector store.
    """

    __tablename__ = "memory_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(32))  # workflow, failure, preference, decision
    website: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    content: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
