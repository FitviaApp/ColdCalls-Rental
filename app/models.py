"""
Database models
"""
import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime,
    ForeignKey, Text, Enum, Index
)
from sqlalchemy.orm import relationship

from app.database import Base


class CampaignStatus(str, enum.Enum):
    DRAFT = "draft"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class CallStatus(str, enum.Enum):
    PENDING = "pending"
    QUEUED = "queued"
    RINGING = "ringing"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PaymentStatus(str, enum.Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FAILED = "failed"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    is_admin = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    credits = Column(Float, default=0.0)

    # Transfer number (3CX) for call transfers
    transfer_number = Column(String(20), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    campaigns = relationship("Campaign", back_populates="user", cascade="all, delete-orphan")
    payments = relationship("Payment", back_populates="user", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<User {self.email}>"


class CallerID(Base):
    __tablename__ = "caller_ids"

    id = Column(Integer, primary_key=True, index=True)
    phone_number = Column(String(20), unique=True, nullable=False)
    country_code = Column(String(5), nullable=False, index=True)
    description = Column(String(255), default="")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    campaigns = relationship("Campaign", back_populates="caller_id")

    def __repr__(self):
        return f"<CallerID {self.phone_number}>"


class Country(Base):
    __tablename__ = "countries"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(5), unique=True, nullable=False)
    name = Column(String(100), nullable=False)
    price_per_minute = Column(Float, nullable=False)
    is_active = Column(Boolean, default=True)

    # Relationships
    campaigns = relationship("Campaign", back_populates="country")

    def __repr__(self):
        return f"<Country {self.code}>"


class Audio(Base):
    __tablename__ = "audios"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    r2_key = Column(String(500), nullable=False)
    r2_url = Column(String(500), nullable=False)
    duration_seconds = Column(Integer, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    campaigns = relationship("Campaign", back_populates="audio")

    def __repr__(self):
        return f"<Audio {self.name}>"


class Campaign(Base):
    __tablename__ = "campaigns"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    caller_id_id = Column(Integer, ForeignKey("caller_ids.id"), nullable=False)
    country_id = Column(Integer, ForeignKey("countries.id"), nullable=False)
    audio_id = Column(Integer, ForeignKey("audios.id"), nullable=False)
    status = Column(Enum(CampaignStatus), default=CampaignStatus.DRAFT, index=True)

    # Progress tracking
    total_numbers = Column(Integer, default=0)
    processed_numbers = Column(Integer, default=0)
    successful_calls = Column(Integer, default=0)
    failed_calls = Column(Integer, default=0)
    total_cost = Column(Float, default=0.0)
    reserved_credits = Column(Float, default=0.0)  # Credits reserved when campaign starts

    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    # Relationships
    user = relationship("User", back_populates="campaigns")
    caller_id = relationship("CallerID", back_populates="campaigns")
    country = relationship("Country", back_populates="campaigns")
    audio = relationship("Audio", back_populates="campaigns")
    numbers = relationship("CampaignNumber", back_populates="campaign", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Campaign {self.name}>"

    @property
    def progress_percent(self) -> float:
        if self.total_numbers == 0:
            return 0.0
        return (self.processed_numbers / self.total_numbers) * 100


class CampaignNumber(Base):
    __tablename__ = "campaign_numbers"

    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id"), nullable=False, index=True)
    phone_number = Column(String(20), nullable=False)
    status = Column(Enum(CallStatus), default=CallStatus.PENDING, index=True)
    call_sid = Column(String(50), nullable=True)
    duration_seconds = Column(Integer, nullable=True)
    cost = Column(Float, nullable=True)
    answered_by = Column(String(50), nullable=True)  # human, machine, unknown
    processed_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)

    # Relationships
    campaign = relationship("Campaign", back_populates="numbers")

    # Index for faster pending number lookup
    __table_args__ = (
        Index('ix_campaign_numbers_campaign_status', 'campaign_id', 'status'),
    )

    def __repr__(self):
        return f"<CampaignNumber {self.phone_number}>"


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    tx_hash = Column(String(100), unique=True, nullable=False)
    amount_usdt = Column(Float, nullable=False)
    credits_added = Column(Float, nullable=False)
    status = Column(Enum(PaymentStatus), default=PaymentStatus.PENDING)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    verified_at = Column(DateTime, nullable=True)

    # Relationships
    user = relationship("User", back_populates="payments")

    def __repr__(self):
        return f"<Payment {self.tx_hash[:10]}...>"


class SystemSetting(Base):
    __tablename__ = "system_settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(100), unique=True, nullable=False, index=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<SystemSetting {self.key}>"
