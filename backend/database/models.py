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
