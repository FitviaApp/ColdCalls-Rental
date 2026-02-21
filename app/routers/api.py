"""
API Router - JSON endpoints for AJAX calls and TwiML
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.config import get_settings
from app.dependencies import require_active_rental
from app.models import User, Campaign, CampaignNumber, CallerID, Country, Audio, CampaignStatus
from app.schemas import DashboardStats, CampaignProgress, DropdownCallerID, DropdownCountry, DropdownAudio
from app.services.rental_service import has_active_rental

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/api", tags=["api"])


# ============== TwiML Endpoint ==============
def _hangup_response() -> Response:
    twiml = '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'
    return Response(content=twiml, media_type="application/xml")


def _build_transfer_block(campaign: Campaign, transfer_number: str) -> str:
    return f'''
    <Dial callerId="{campaign.caller_id.phone_number}" timeout="30">
        <Number>{transfer_number}</Number>
    </Dial>
'''


@router.post("/twiml/{campaign_id}")
@router.get("/twiml/{campaign_id}")
async def twiml_handler(
    campaign_id: int,
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Dynamic TwiML endpoint for handling calls with machine detection.

    Twilio calls this URL when the call is answered.
    - If human: Play audio, then transfer to 3CX number
    - If machine: Hang up

    Twilio sends AnsweredBy parameter:
    - human
    - machine_start, machine_end_beep, machine_end_silence, machine_end_other
    - fax
    - unknown
    """
    # Parse form data from Twilio (POST) or query params (GET)
    if request.method == "POST":
        form_data = await request.form()
        answered_by = form_data.get("AnsweredBy", "")
    else:
        answered_by = request.query_params.get("AnsweredBy", "")

    # Get campaign
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()

    if not campaign:
        return _hangup_response()

    if not has_active_rental(db, campaign.user_id):
        return _hangup_response()

    # Get transfer number from user settings
    transfer_number = campaign.user.transfer_number

    if not transfer_number:
        return _hangup_response()

    # Log the request for debugging
    logger.info(f"TwiML request for campaign {campaign_id}: AnsweredBy={answered_by}")

    # Check if answered by machine (any machine_* value)
    if answered_by.startswith("machine") or answered_by == "fax":
        # Machine/voicemail/fax detected - hang up
        logger.info(f"Campaign {campaign_id}: Machine detected ({answered_by}), hanging up")
        return _hangup_response()
    if campaign.press_1_to_talk_with_agent:
        base_url = settings.BASE_URL.rstrip("/")
        logger.info(
            f"Campaign {campaign_id}: Human/unknown ({answered_by}), "
            "playing audio and waiting for DTMF 1"
        )
        twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play>{campaign.audio.r2_url}</Play>
    <Gather input="dtmf" numDigits="1" timeout="8" action="{base_url}/api/twiml/{campaign_id}/gather" method="POST" actionOnEmptyResult="true">
        <Say voice="alice">Press 1 to talk with an agent.</Say>
    </Gather>
    <Hangup/>
</Response>'''
    else:
        # Human answered (or unknown - treat as human to not miss calls)
        # Play the campaign audio, then transfer to 3CX
        logger.info(
            f"Campaign {campaign_id}: Human/unknown ({answered_by}), "
            f"playing audio and transferring to {transfer_number}"
        )
        twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play>{campaign.audio.r2_url}</Play>
    {_build_transfer_block(campaign, transfer_number)}
</Response>'''

    return Response(content=twiml, media_type="application/xml")


@router.post("/twiml/{campaign_id}/gather")
async def twiml_gather_handler(
    campaign_id: int,
    request: Request,
    db: Session = Depends(get_db)
):
    form_data = await request.form()
    digits = str(form_data.get("Digits", "")).strip()

    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not campaign:
        return _hangup_response()
    if not has_active_rental(db, campaign.user_id):
        return _hangup_response()

    transfer_number = campaign.user.transfer_number
    if not transfer_number:
        return _hangup_response()

    if digits == "1":
        logger.info(f"Campaign {campaign_id}: DTMF 1 received, transferring")
        twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    {_build_transfer_block(campaign, transfer_number)}
</Response>'''
        return Response(content=twiml, media_type="application/xml")

    logger.info(f"Campaign {campaign_id}: Invalid/no DTMF ({digits}), hanging up")
    return _hangup_response()


@router.get("/stats", response_model=DashboardStats)
async def get_stats(
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    """Get dashboard statistics"""
    campaigns = db.query(Campaign).filter(Campaign.user_id == user.id).all()

    return DashboardStats(
        total_campaigns=len(campaigns),
        active_campaigns=len([c for c in campaigns if c.status == CampaignStatus.RUNNING]),
        total_calls=sum(c.processed_numbers for c in campaigns),
        successful_calls=sum(c.successful_calls for c in campaigns),
        total_spent=sum(c.total_cost for c in campaigns)
    )


@router.get("/campaigns/{campaign_id}/progress", response_model=CampaignProgress)
async def get_campaign_progress(
    campaign_id: int,
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    """Get campaign progress for real-time updates"""
    campaign = db.query(Campaign).filter(
        Campaign.id == campaign_id,
        Campaign.user_id == user.id
    ).first()

    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    return CampaignProgress(
        status=campaign.status,
        total=campaign.total_numbers,
        processed=campaign.processed_numbers,
        successful=campaign.successful_calls,
        failed=campaign.failed_calls,
        cost=campaign.total_cost,
        progress_percent=campaign.progress_percent
    )


@router.get("/campaigns/{campaign_id}/numbers")
async def get_campaign_numbers(
    campaign_id: int,
    page: int = 1,
    per_page: int = 50,
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    """Get paginated list of campaign numbers"""
    campaign = db.query(Campaign).filter(
        Campaign.id == campaign_id,
        Campaign.user_id == user.id
    ).first()

    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    offset = (page - 1) * per_page
    numbers = db.query(CampaignNumber).filter(
        CampaignNumber.campaign_id == campaign_id
    ).order_by(CampaignNumber.id).offset(offset).limit(per_page).all()

    total = db.query(CampaignNumber).filter(
        CampaignNumber.campaign_id == campaign_id
    ).count()

    return {
        "numbers": [
            {
                "id": n.id,
                "phone_number": n.phone_number,
                "status": n.status.value,
                "duration_seconds": n.duration_seconds,
                "cost": n.cost,
                "answered_by": n.answered_by,
                "processed_at": n.processed_at.isoformat() if n.processed_at else None
            }
            for n in numbers
        ],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page
    }


# ============== Dropdown Data ==============

@router.get("/data/caller-ids", response_model=list[DropdownCallerID])
async def get_caller_ids(
    country: str = None,
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    """Get active caller IDs, optionally filtered by country"""
    query = db.query(CallerID).filter(
        CallerID.is_active == True,
        CallerID.user_id == user.id
    )

    if country:
        query = query.filter(CallerID.country_code == country.upper())

    return query.order_by(CallerID.phone_number).all()


@router.get("/data/countries", response_model=list[DropdownCountry])
async def get_countries(
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    """Get active countries"""
    return db.query(Country).filter(
        Country.is_active == True
    ).order_by(Country.name).all()


@router.get("/data/audios", response_model=list[DropdownAudio])
async def get_audios(
    user: User = Depends(require_active_rental),
    db: Session = Depends(get_db)
):
    """Get active audios"""
    return db.query(Audio).filter(
        Audio.is_active == True,
        Audio.user_id == user.id
    ).order_by(Audio.name).all()
