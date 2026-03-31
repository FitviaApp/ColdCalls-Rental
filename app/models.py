"""
Database models
"""
import enum
from datetime import datetime

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


class RentalStatus(str, enum.Enum):
    ACTIVE = "active"
    EXPIRED = "expired"


class VoiceProvider(str, enum.Enum):
    TWILIO = "twilio"
    SIGNALWIRE = "signalwire"
    TELNYX = "telnyx"
    VONAGE = "vonage"
    VOXIMPLANT = "voximplant"


class CampaignMode(str, enum.Enum):
    AUDIO = "audio"
    AI_AGENT = "ai_agent"


class AIAgentRuntimeProvider(str, enum.Enum):
    LEGACY_OPENAI = "legacy_openai"
    ELEVENLABS_AGENT = "elevenlabs_agent"


class VoxCallerIDVerificationStatus(str, enum.Enum):
    NOT_STARTED = "not_started"
    PENDING = "pending"
    VERIFIED = "verified"
    FAILED = "failed"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    is_admin = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)

    # Transfer number (3CX) for call transfers
    transfer_number = Column(String(20), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    campaigns = relationship("Campaign", back_populates="user", cascade="all, delete-orphan")
    rental_payments = relationship("RentalPayment", cascade="all, delete-orphan")
    rentals = relationship("UserRental", cascade="all, delete-orphan")
    caller_ids = relationship("CallerID", back_populates="user", cascade="all, delete-orphan")
    audios = relationship("Audio", back_populates="user", cascade="all, delete-orphan")
    twilio_credentials = relationship(
        "UserTwilioCredential",
        back_populates="user",
        cascade="all, delete-orphan",
        uselist=False
    )
    signalwire_credentials = relationship(
        "UserSignalWireCredential",
        back_populates="user",
        cascade="all, delete-orphan",
        uselist=False
    )
    telnyx_credentials = relationship(
        "UserTelnyxCredential",
        back_populates="user",
        cascade="all, delete-orphan",
        uselist=False
    )
    vonage_credentials = relationship(
        "UserVonageCredential",
        back_populates="user",
        cascade="all, delete-orphan",
        uselist=False
    )
    voximplant_credentials = relationship(
        "UserVoximplantCredential",
        back_populates="user",
        cascade="all, delete-orphan",
        uselist=False
    )
    openai_credentials = relationship(
        "UserOpenAICredential",
        back_populates="user",
        cascade="all, delete-orphan",
        uselist=False
    )
    elevenlabs_credentials = relationship(
        "UserElevenLabsCredential",
        back_populates="user",
        cascade="all, delete-orphan",
        uselist=False
    )
    ai_agents = relationship("AIAgent", back_populates="user", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<User {self.email}>"


class CallerID(Base):
    __tablename__ = "caller_ids"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    phone_number = Column(String(20), unique=True, nullable=False)
    country_code = Column(String(5), nullable=False, index=True)
    description = Column(String(255), default="")
    is_active = Column(Boolean, default=True)
    vox_callerid_id = Column(Integer, nullable=True, index=True)
    elevenlabs_phone_number_id = Column(String(120), nullable=True, index=True)
    vox_verification_status = Column(
        Enum(
            VoxCallerIDVerificationStatus,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=VoxCallerIDVerificationStatus.NOT_STARTED,
        nullable=False,
        index=True,
    )
    vox_last_verification_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    user = relationship("User", back_populates="caller_ids")
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
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    name = Column(String(255), nullable=False)
    r2_key = Column(String(500), nullable=False)
    r2_url = Column(String(500), nullable=False)
    duration_seconds = Column(Integer, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    user = relationship("User", back_populates="audios")
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
    audio_id = Column(Integer, ForeignKey("audios.id"), nullable=True)
    ai_agent_id = Column(Integer, ForeignKey("ai_agents.id"), nullable=True, index=True)
    campaign_mode = Column(
        Enum(CampaignMode, values_callable=lambda enum_cls: [member.value for member in enum_cls]),
        default=CampaignMode.AUDIO,
        nullable=False,
        index=True,
    )
    status = Column(Enum(CampaignStatus), default=CampaignStatus.DRAFT, index=True)
    press_1_to_talk_with_agent = Column(Boolean, default=False, nullable=False)
    voice_provider = Column(
        Enum(VoiceProvider, values_callable=lambda enum_cls: [member.value for member in enum_cls]),
        default=VoiceProvider.TWILIO,
        nullable=False,
        index=True
    )
    max_concurrent_calls = Column(Integer, default=1, nullable=False)

    # Progress tracking
    total_numbers = Column(Integer, default=0)
    processed_numbers = Column(Integer, default=0)
    successful_calls = Column(Integer, default=0)
    failed_calls = Column(Integer, default=0)
    total_cost = Column(Float, default=0.0)

    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    # Relationships
    user = relationship("User", back_populates="campaigns")
    caller_id = relationship("CallerID", back_populates="campaigns")
    country = relationship("Country", back_populates="campaigns")
    audio = relationship("Audio", back_populates="campaigns")
    ai_agent = relationship("AIAgent", back_populates="campaigns")
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
    lead_name = Column(String(255), nullable=True)
    lead_variables_json = Column(Text, nullable=True)
    status = Column(Enum(CallStatus), default=CallStatus.PENDING, index=True)
    call_sid = Column(String(50), nullable=True)
    duration_seconds = Column(Integer, nullable=True)
    cost = Column(Float, nullable=True)
    answered_by = Column(String(50), nullable=True)  # human, machine, unknown
    ai_turn_count = Column(Integer, nullable=True)
    ai_no_input_turns = Column(Integer, nullable=True)
    ai_last_user_input = Column(Text, nullable=True)
    ai_last_assistant_text = Column(Text, nullable=True)
    ai_handoff_reason = Column(Text, nullable=True)
    ai_runtime_error = Column(Text, nullable=True)
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


class UserTwilioCredential(Base):
    __tablename__ = "user_twilio_credentials"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True, index=True)
    account_sid_encrypted = Column(Text, nullable=False)
    auth_token_encrypted = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="twilio_credentials")

    def __repr__(self):
        return f"<UserTwilioCredential user_id={self.user_id}>"


class UserSignalWireCredential(Base):
    __tablename__ = "user_signalwire_credentials"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True, index=True)
    project_id_encrypted = Column(Text, nullable=False)
    api_token_encrypted = Column(Text, nullable=False)
    space_url_encrypted = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="signalwire_credentials")

    def __repr__(self):
        return f"<UserSignalWireCredential user_id={self.user_id}>"


class UserTelnyxCredential(Base):
    __tablename__ = "user_telnyx_credentials"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True, index=True)
    api_key_encrypted = Column(Text, nullable=False)
    account_sid_encrypted = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="telnyx_credentials")

    def __repr__(self):
        return f"<UserTelnyxCredential user_id={self.user_id}>"


class UserVonageCredential(Base):
    __tablename__ = "user_vonage_credentials"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True, index=True)
    application_id_encrypted = Column(Text, nullable=False)
    private_key_encrypted = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="vonage_credentials")

    def __repr__(self):
        return f"<UserVonageCredential user_id={self.user_id}>"


class UserVoximplantCredential(Base):
    __tablename__ = "user_voximplant_credentials"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True, index=True)
    account_id_encrypted = Column(Text, nullable=False)
    application_id_encrypted = Column(Text, nullable=True)
    service_account_email_encrypted = Column(Text, nullable=False)
    key_id_encrypted = Column(Text, nullable=False)
    private_key_encrypted = Column(Text, nullable=False)
    vox_app_id = Column(Integer, nullable=True)
    vox_rule_id = Column(Integer, nullable=True)
    vox_scenario_id = Column(Integer, nullable=True)
    provision_status = Column(String(30), nullable=False, default="pending", index=True)
    provision_error = Column(Text, nullable=True)
    provisioned_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="voximplant_credentials")

    def __repr__(self):
        return f"<UserVoximplantCredential user_id={self.user_id}>"


class UserOpenAICredential(Base):
    __tablename__ = "user_openai_credentials"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True, index=True)
    api_key_encrypted = Column(Text, nullable=False)
    organization_id_encrypted = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="openai_credentials")

    def __repr__(self):
        return f"<UserOpenAICredential user_id={self.user_id}>"


class UserElevenLabsCredential(Base):
    __tablename__ = "user_elevenlabs_credentials"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True, index=True)
    api_key_encrypted = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="elevenlabs_credentials")

    def __repr__(self):
        return f"<UserElevenLabsCredential user_id={self.user_id}>"


class AIAgent(Base):
    __tablename__ = "ai_agents"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    system_prompt = Column(Text, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False, index=True)
    language = Column(String(10), default="en", nullable=False)
    voice_id = Column(String(100), nullable=False)
    model = Column(String(100), nullable=False, default="gpt-4o-mini")
    temperature = Column(Float, nullable=False, default=0.7)
    runtime_provider = Column(
        Enum(
            AIAgentRuntimeProvider,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=AIAgentRuntimeProvider.LEGACY_OPENAI,
        index=True,
    )
    external_agent_id = Column(String(120), nullable=True, index=True)
    last_sync_status = Column(Text, nullable=True)
    handoff_description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="ai_agents")
    campaigns = relationship("Campaign", back_populates="ai_agent")

    def __repr__(self):
        return f"<AIAgent {self.name}>"


class RentalPlan(Base):
    __tablename__ = "rental_plans"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(30), unique=True, nullable=False, index=True)
    name = Column(String(100), nullable=False)
    duration_days = Column(Integer, nullable=False)
    price_usdt = Column(Float, nullable=False)
    is_active = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    rentals = relationship("UserRental", back_populates="plan")
    payments = relationship("RentalPayment", back_populates="plan")

    def __repr__(self):
        return f"<RentalPlan {self.code}>"


class UserRental(Base):
    __tablename__ = "user_rentals"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    plan_id = Column(Integer, ForeignKey("rental_plans.id"), nullable=False, index=True)
    starts_at = Column(DateTime, nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False, index=True)
    status = Column(Enum(RentalStatus), default=RentalStatus.ACTIVE, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User")
    plan = relationship("RentalPlan", back_populates="rentals")
    payment = relationship("RentalPayment", back_populates="rental", uselist=False)

    __table_args__ = (
        Index("ix_user_rentals_user_status_expires", "user_id", "status", "expires_at"),
    )

    def __repr__(self):
        return f"<UserRental user_id={self.user_id} expires_at={self.expires_at}>"


class RentalPayment(Base):
    __tablename__ = "rental_payments"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    plan_id = Column(Integer, ForeignKey("rental_plans.id"), nullable=False, index=True)
    rental_id = Column(Integer, ForeignKey("user_rentals.id"), nullable=True, index=True)
    tx_hash = Column(String(100), unique=True, nullable=False)
    amount_usdt = Column(Float, nullable=False, default=0.0)
    status = Column(Enum(PaymentStatus), default=PaymentStatus.PENDING, index=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    verified_at = Column(DateTime, nullable=True)

    user = relationship("User")
    plan = relationship("RentalPlan", back_populates="payments")
    rental = relationship("UserRental", back_populates="payment")

    def __repr__(self):
        return f"<RentalPayment {self.tx_hash[:10]}...>"
