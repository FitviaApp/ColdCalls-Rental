"""
Pydantic schemas for request/response validation
"""
from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, EmailStr, Field

from app.models import CampaignStatus, CallStatus, PaymentStatus


# ============== User Schemas ==============

class UserBase(BaseModel):
    email: EmailStr


class UserCreate(UserBase):
    password: str = Field(..., min_length=6)


class UserLogin(UserBase):
    password: str


class UserUpdate(BaseModel):
    transfer_number: Optional[str] = None


class UserResponse(UserBase):
    id: int
    is_admin: bool
    is_active: bool
    credits: float
    transfer_number: Optional[str]
    transfer_configured: bool = False
    created_at: datetime

    model_config = {"from_attributes": True}

    def __init__(self, **data):
        # Compute transfer_configured from transfer_number
        if 'transfer_number' in data:
            data['transfer_configured'] = bool(data['transfer_number'])
        super().__init__(**data)


class UserProfile(UserResponse):
    pass


# ============== CallerID Schemas ==============

class CallerIDBase(BaseModel):
    phone_number: str = Field(..., pattern=r'^\+[1-9]\d{1,14}$')
    country_code: str = Field(..., min_length=2, max_length=5)
    description: str = ""


class CallerIDCreate(CallerIDBase):
    pass


class CallerIDUpdate(BaseModel):
    phone_number: Optional[str] = Field(None, pattern=r'^\+[1-9]\d{1,14}$')
    country_code: Optional[str] = Field(None, min_length=2, max_length=5)
    description: Optional[str] = None
    is_active: Optional[bool] = None


class CallerIDResponse(CallerIDBase):
    id: int
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# ============== Country Schemas ==============

class CountryBase(BaseModel):
    code: str = Field(..., min_length=2, max_length=5)
    name: str = Field(..., min_length=2, max_length=100)
    price_per_minute: float = Field(..., ge=0)


class CountryCreate(CountryBase):
    pass


class CountryUpdate(BaseModel):
    code: Optional[str] = Field(None, min_length=2, max_length=5)
    name: Optional[str] = Field(None, min_length=2, max_length=100)
    price_per_minute: Optional[float] = Field(None, ge=0)
    is_active: Optional[bool] = None


class CountryResponse(CountryBase):
    id: int
    is_active: bool

    model_config = {"from_attributes": True}


# ============== Audio Schemas ==============

class AudioBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)


class AudioCreate(AudioBase):
    r2_key: str
    r2_url: str
    duration_seconds: Optional[int] = None


class AudioUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    is_active: Optional[bool] = None


class AudioResponse(AudioBase):
    id: int
    r2_key: str
    r2_url: str
    duration_seconds: Optional[int]
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# ============== Campaign Schemas ==============

class CampaignBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)


class CampaignCreate(CampaignBase):
    caller_id_id: int
    country_id: int
    audio_id: int
    phone_numbers: List[str]  # List of phone numbers to call


class CampaignUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)


class CampaignResponse(CampaignBase):
    id: int
    user_id: int
    caller_id_id: int
    country_id: int
    audio_id: int
    status: CampaignStatus
    total_numbers: int
    processed_numbers: int
    successful_calls: int
    failed_calls: int
    total_cost: float
    progress_percent: float
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]

    model_config = {"from_attributes": True}


class CampaignDetail(CampaignResponse):
    caller_id: CallerIDResponse
    country: CountryResponse
    audio: AudioResponse


# ============== CampaignNumber Schemas ==============

class CampaignNumberResponse(BaseModel):
    id: int
    phone_number: str
    status: CallStatus
    call_sid: Optional[str]
    duration_seconds: Optional[int]
    cost: Optional[float]
    answered_by: Optional[str]
    processed_at: Optional[datetime]
    error_message: Optional[str]

    model_config = {"from_attributes": True}


# ============== Payment Schemas ==============

class PaymentVerify(BaseModel):
    tx_hash: str = Field(..., min_length=64, max_length=70)


class PaymentResponse(BaseModel):
    id: int
    tx_hash: str
    amount_usdt: float
    credits_added: float
    status: PaymentStatus
    created_at: datetime
    verified_at: Optional[datetime]

    model_config = {"from_attributes": True}


# ============== Stats Schemas ==============

class DashboardStats(BaseModel):
    credits: float
    total_campaigns: int
    active_campaigns: int
    total_calls: int
    successful_calls: int
    total_spent: float


class CampaignProgress(BaseModel):
    status: CampaignStatus
    total: int
    processed: int
    successful: int
    failed: int
    cost: float
    progress_percent: float


# ============== Dropdown Data Schemas ==============

class DropdownCallerID(BaseModel):
    id: int
    phone_number: str
    description: str

    model_config = {"from_attributes": True}


class DropdownCountry(BaseModel):
    id: int
    code: str
    name: str
    price_per_minute: float

    model_config = {"from_attributes": True}


class DropdownAudio(BaseModel):
    id: int
    name: str
    duration_seconds: Optional[int]

    model_config = {"from_attributes": True}
