"""
Twilio Service - Refactored from cold_calls.py
Handles call initiation, status polling, and machine detection
"""
import time
import logging
from typing import Optional

from twilio.rest import Client

from app.database import SessionLocal
from app.services.system_settings_service import get_twilio_credentials

logger = logging.getLogger(__name__)


class TwilioService:
    """Service for making Twilio calls with machine detection"""

    def __init__(self):
        """
        Initialize Twilio client with global credentials from settings
        """
        db = SessionLocal()
        try:
            sid, token = get_twilio_credentials(db)
        finally:
            db.close()

        if not sid or not token:
            raise ValueError("Twilio credentials not configured")

        self.account_sid = sid
        self.auth_token = token
        self.client = Client(self.account_sid, self.auth_token)

    def make_call(
        self,
        to_number: str,
        from_number: str,
        audio_url: str,
        transfer_number: str,
        timeout: int = 60
    ) -> dict:
        """
        Initiate a call with machine detection enabled

        Args:
            to_number: Destination phone number (E.164 format)
            from_number: Caller ID (E.164 format)
            audio_url: URL of the audio file to play
            transfer_number: Number to transfer to (3CX)
            timeout: Ring timeout in seconds

        Returns:
            dict with 'call_sid' and 'status'
        """
        logger.info(f"Initiating call to {to_number} from {from_number}")

        # Build TwiML inline - plays audio then transfers
        twiml = f'''<Response>
            <Play>{audio_url}</Play>
            <Dial callerId="{from_number}" timeout="30">
                <Number>{transfer_number}</Number>
            </Dial>
        </Response>'''

        call = self.client.calls.create(
            to=to_number,
            from_=from_number,
            twiml=twiml,
            timeout=timeout,
            # Machine detection parameters (same as cold_calls.py)
            machine_detection='Enable',
            machine_detection_timeout=5,
            machine_detection_speech_threshold=2400,
            machine_detection_speech_end_threshold=1200,
            machine_detection_silence_timeout=5000
        )

        logger.info(f"Call initiated: SID={call.sid}, status={call.status}")

        return {
            'call_sid': call.sid,
            'status': call.status
        }

    def poll_call_status(
        self,
        call_sid: str,
        max_wait: int = 70,
        poll_interval: int = 2
    ) -> dict:
        """
        Poll call status until completion or timeout

        Args:
            call_sid: Twilio call SID
            max_wait: Maximum wait time in seconds
            poll_interval: Time between polls in seconds

        Returns:
            dict with 'status', 'duration', 'answered_by'
        """
        elapsed = 0
        final_statuses = ['completed', 'failed', 'busy', 'no-answer', 'canceled']
        last_status = None

        while elapsed < max_wait:
            try:
                call = self.client.calls(call_sid).fetch()
                current_status = call.status

                if current_status != last_status:
                    logger.debug(f"Call {call_sid}: status={current_status}")
                    last_status = current_status

                if current_status in final_statuses:
                    return {
                        'status': current_status,
                        'duration': int(call.duration) if call.duration else 0,
                        'answered_by': getattr(call, 'answered_by', None)
                    }

                time.sleep(poll_interval)
                elapsed += poll_interval

            except Exception as e:
                logger.warning(f"Polling error for {call_sid}: {e}")
                time.sleep(poll_interval)
                elapsed += poll_interval

        logger.warning(f"Polling timeout for call {call_sid}")
        return {
            'status': 'timeout',
            'duration': 0,
            'answered_by': None
        }

    def get_call_cost(self, call_sid: str) -> float:
        """
        Get the cost of a completed call

        Args:
            call_sid: Twilio call SID

        Returns:
            Cost in USD (positive number)
        """
        try:
            call = self.client.calls(call_sid).fetch()
            # Twilio returns price as negative string
            price = float(call.price or 0)
            return abs(price)
        except Exception as e:
            logger.error(f"Error getting call cost for {call_sid}: {e}")
            return 0.0

    def get_call_details(self, call_sid: str) -> Optional[dict]:
        """
        Get detailed information about a call

        Args:
            call_sid: Twilio call SID

        Returns:
            dict with call details or None
        """
        try:
            call = self.client.calls(call_sid).fetch()
            return {
                'sid': call.sid,
                'status': call.status,
                'duration': int(call.duration) if call.duration else 0,
                'price': abs(float(call.price or 0)),
                'answered_by': getattr(call, 'answered_by', None),
                'start_time': call.start_time,
                'end_time': call.end_time
            }
        except Exception as e:
            logger.error(f"Error fetching call details for {call_sid}: {e}")
            return None
