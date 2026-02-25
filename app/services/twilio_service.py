"""
Twilio Service - Refactored from cold_calls.py
Handles call initiation, status polling, and machine detection
"""
import time
import logging
from typing import Optional, Callable

from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class TwilioService:
    """Service for making Twilio calls with machine detection"""

    def __init__(self, account_sid: str, auth_token: str):
        """
        Initialize Twilio client with explicit user credentials.
        """
        if not account_sid or not auth_token:
            raise ValueError("Twilio credentials not configured")

        self.account_sid = account_sid
        self.auth_token = auth_token
        self.client = Client(self.account_sid, self.auth_token)

    def make_call(
        self,
        to_number: str,
        from_number: str,
        audio_url: str,
        transfer_number: str,
        campaign_id: Optional[int] = None,
        press_1_to_talk_with_agent: bool = False,
        timeout: int = 60
    ) -> dict:
        """
        Initiate a call.

        Args:
            to_number: Destination phone number (E.164 format)
            from_number: Caller ID (E.164 format)
            audio_url: URL of the audio file to play
            transfer_number: Number to transfer to (3CX)
            campaign_id: Campaign ID for dynamic TwiML callback endpoint
            press_1_to_talk_with_agent: If true, require DTMF "1" before transfer
            timeout: Ring timeout in seconds

        Returns:
            dict with 'call_sid' and 'status'
        """
        logger.info(f"Initiating call to {to_number} from {from_number}")

        # Use dynamic callback URL only when "Press 1" is enabled.
        # For normal calls, inline TwiML avoids dependency on public BASE_URL.
        call_kwargs = {}
        if press_1_to_talk_with_agent:
            if campaign_id is None:
                raise ValueError("campaign_id is required when press_1_to_talk_with_agent is enabled")
            base_url = settings.BASE_URL.rstrip("/")
            call_kwargs["url"] = f"{base_url}/api/twiml/{campaign_id}"
        else:
            twiml = f'''<Response>
            <Play>{audio_url}</Play>
            <Dial callerId="{from_number}" timeout="30">
                <Number>{transfer_number}</Number>
            </Dial>
        </Response>'''
            call_kwargs["twiml"] = twiml

        # Keep AMD for direct transfer campaigns, but skip it on Press 1 flow.
        # In Press 1 flow, DTMF confirmation already acts as a human gate.
        machine_detection_kwargs = {}
        if not press_1_to_talk_with_agent:
            machine_detection_kwargs = {
                "machine_detection": "Enable",
                "machine_detection_timeout": 5,
                "machine_detection_speech_threshold": 2400,
                "machine_detection_speech_end_threshold": 1200,
                "machine_detection_silence_timeout": 5000,
            }

        try:
            call = self.client.calls.create(
                to=to_number,
                from_=from_number,
                **call_kwargs,
                timeout=timeout,
                **machine_detection_kwargs,
            )
        except TwilioRestException as e:
            error_code = getattr(e, "code", None)
            more_info = ""
            details = getattr(e, "details", None)
            if isinstance(details, dict):
                more_info = str(details.get("more_info", "") or "")
            if not more_info:
                more_info = str(getattr(e, "more_info", "") or "")
            if not more_info and error_code:
                more_info = f"https://www.twilio.com/docs/errors/{error_code}"

            # Surface Twilio-native diagnostics in campaign error_message.
            detail = (
                f"Twilio create call failed: status={e.status} code={e.code} "
                f"message={e.msg} more_info={more_info} to={to_number} from={from_number} "
                f"account_sid={self.account_sid}"
            )
            logger.error(detail)
            raise RuntimeError(detail) from e

        logger.info(f"Call initiated: SID={call.sid}, status={call.status}")

        return {
            'call_sid': call.sid,
            'status': call.status
        }

    def poll_call_status(
        self,
        call_sid: str,
        max_wait: int = 70,
        poll_interval: int = 2,
        status_callback: Optional[Callable[[str, int, Optional[str]], None]] = None,
    ) -> dict:
        """
        Poll call status until completion or timeout

        Args:
            call_sid: Twilio call SID
            max_wait: Maximum wait time in seconds
            poll_interval: Time between polls in seconds
            status_callback: Optional callback invoked when status changes

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
                    if status_callback:
                        try:
                            status_callback(
                                current_status,
                                int(call.duration) if call.duration else 0,
                                getattr(call, 'answered_by', None),
                            )
                        except Exception as callback_error:
                            logger.warning(
                                f"Status callback error for {call_sid}: {callback_error}"
                            )
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

    def get_account_balance(self) -> Optional[dict]:
        """
        Get account balance from Twilio.

        Returns:
            dict with 'balance' and 'currency', or None on error.
        """
        try:
            balance_info = self.client.balance.fetch()
            return {
                "balance": float(balance_info.balance),
                "currency": str(balance_info.currency or "USD"),
            }
        except Exception as e:
            logger.error(f"Error fetching Twilio account balance: {e}")
            return None
