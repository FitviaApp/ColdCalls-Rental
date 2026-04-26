import os
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import auth as auth_module
from app.database import Base, get_db
from app.dependencies import require_active_rental
from app.models import (
    AIAgent,
    Audio,
    CallStatus,
    Campaign,
    CampaignMode,
    CampaignNumber,
    CampaignStatus,
    CallerID,
    Country,
    User,
    VoiceProvider,
)
from app.routers import api as api_router_module
from app.routers import campaigns as campaigns_router_module
from app.services.ai_call_runtime_service import AICallRuntimeService
from app.services.user_elevenlabs_service import upsert_user_elevenlabs_credentials
from app.services.user_openai_service import upsert_user_openai_credentials
from app.services.user_signalwire_service import upsert_user_signalwire_credentials


class AICampaignRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.original_encryption_key = auth_module.settings.ENCRYPTION_KEY
        auth_module.settings.ENCRYPTION_KEY = Fernet.generate_key().decode()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False},
        )
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        Base.metadata.create_all(bind=self.engine)

        self.db = self.SessionLocal()
        self.user = User(
            email="user@example.com",
            password_hash="hashed",
            is_admin=False,
            is_active=True,
            transfer_number="+15550001111",
        )
        self.db.add(self.user)
        self.db.flush()

        self.caller_id = CallerID(
            user_id=self.user.id,
            phone_number="+15557654321",
            country_code="US",
            description="Main line",
            is_active=True,
        )
        self.db.add(self.caller_id)

        self.country = Country(
            code="US",
            name="United States",
            price_per_minute=0.02,
            is_active=True,
        )
        self.db.add(self.country)

        self.audio = Audio(
            user_id=self.user.id,
            name="Intro",
            r2_key="intro.mp3",
            r2_url="https://example.com/intro.mp3",
            duration_seconds=12,
            is_active=True,
        )
        self.db.add(self.audio)

        self.ai_agent = AIAgent(
            user_id=self.user.id,
            name="Qualifier",
            system_prompt="Qualify the lead and transfer on interest.",
            is_active=True,
            language="en",
            voice_id="voice_123",
            model="gpt-4o-mini",
            temperature=0.4,
            handoff_description="Transfer when there is strong buying intent.",
        )
        self.db.add(self.ai_agent)
        self.db.flush()

        upsert_user_signalwire_credentials(
            self.db,
            self.user.id,
            project_id="project",
            api_token="token",
            space_url="example.signalwire.com",
        )
        upsert_user_openai_credentials(self.db, self.user.id, "sk-test-1234567890")
        upsert_user_elevenlabs_credentials(self.db, self.user.id, "elevenlabs-key-1234567890")
        self.db.commit()

        self.app = FastAPI()
        self.app.include_router(campaigns_router_module.router)
        self.app.include_router(api_router_module.router)

        def override_get_db():
            db = self.SessionLocal()
            try:
                yield db
            finally:
                db.close()

        async def override_require_active_rental():
            db = self.SessionLocal()
            try:
                return db.query(User).filter(User.id == self.user.id).first()
            finally:
                db.close()

        self.app.dependency_overrides[get_db] = override_get_db
        self.app.dependency_overrides[require_active_rental] = override_require_active_rental
        self.client = TestClient(self.app)

        self.original_worker_check = campaigns_router_module._is_worker_online
        campaigns_router_module._is_worker_online = lambda max_age_seconds=60: True
        self.original_campaign_base_url = campaigns_router_module.settings.BASE_URL
        campaigns_router_module.settings.BASE_URL = "https://app.example.com"

    def tearDown(self):
        auth_module.settings.ENCRYPTION_KEY = self.original_encryption_key
        campaigns_router_module._is_worker_online = self.original_worker_check
        campaigns_router_module.settings.BASE_URL = self.original_campaign_base_url
        self.client.close()
        self.db.close()
        self.engine.dispose()
        self.tmpdir.cleanup()

    def test_create_ai_agent_campaign(self):
        response = self.client.post(
            "/campaigns/create",
            data={
                "name": "AI Campaign",
                "caller_id_id": str(self.caller_id.id),
                "campaign_mode": CampaignMode.AI_AGENT.value,
                "ai_agent_id": str(self.ai_agent.id),
                "voice_provider": VoiceProvider.SIGNALWIRE.value,
                "max_concurrent_calls": "2",
                "numbers_text": "phone,name\n+15551234567,John Doe\n+15559876543,Maria Silva",
            },
            files={},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)

        db = self.SessionLocal()
        try:
            campaign = db.query(Campaign).filter(Campaign.name == "AI Campaign").first()
            self.assertIsNotNone(campaign)
            self.assertEqual(campaign.campaign_mode, CampaignMode.AI_AGENT)
            self.assertEqual(campaign.ai_agent_id, self.ai_agent.id)
            self.assertIsNone(campaign.audio_id)
            self.assertEqual(campaign.total_numbers, 2)
            leads = db.query(CampaignNumber).filter(
                CampaignNumber.campaign_id == campaign.id
            ).order_by(CampaignNumber.phone_number).all()
            self.assertEqual([lead.lead_name for lead in leads], ["John Doe", "Maria Silva"])
        finally:
            db.close()

    def test_start_ai_agent_campaign(self):
        campaign = Campaign(
            user_id=self.user.id,
            name="Startable AI Campaign",
            caller_id_id=self.caller_id.id,
            country_id=self.country.id,
            ai_agent_id=self.ai_agent.id,
            audio_id=None,
            campaign_mode=CampaignMode.AI_AGENT,
            voice_provider=VoiceProvider.SIGNALWIRE,
            press_1_to_talk_with_agent=False,
            max_concurrent_calls=1,
            status=CampaignStatus.DRAFT,
            total_numbers=1,
        )
        self.db.add(campaign)
        self.db.flush()
        self.db.add(
            CampaignNumber(
                campaign_id=campaign.id,
                phone_number="+15551234567",
                status=CallStatus.PENDING,
            )
        )
        self.db.commit()

        response = self.client.post(
            f"/campaigns/{campaign.id}/start",
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)

        db = self.SessionLocal()
        try:
            refreshed = db.query(Campaign).filter(Campaign.id == campaign.id).first()
            self.assertEqual(refreshed.status, CampaignStatus.RUNNING)
            self.assertIsNotNone(refreshed.started_at)
        finally:
            db.close()

    def test_campaign_numbers_endpoint_returns_ai_observability_fields(self):
        campaign = Campaign(
            user_id=self.user.id,
            name="Observed AI Campaign",
            caller_id_id=self.caller_id.id,
            country_id=self.country.id,
            ai_agent_id=self.ai_agent.id,
            audio_id=None,
            campaign_mode=CampaignMode.AI_AGENT,
            voice_provider=VoiceProvider.SIGNALWIRE,
            press_1_to_talk_with_agent=False,
            max_concurrent_calls=1,
            status=CampaignStatus.PAUSED,
            total_numbers=1,
            processed_numbers=1,
        )
        self.db.add(campaign)
        self.db.flush()
        number = CampaignNumber(
            campaign_id=campaign.id,
            phone_number="+15551234567",
            lead_name="John Doe",
            status=CallStatus.COMPLETED,
            ai_turn_count=3,
            ai_no_input_turns=1,
            ai_last_user_input="Tell me more.",
            ai_last_assistant_text="Let me connect you now.",
            ai_handoff_reason="Strong purchase intent",
            ai_runtime_error=None,
        )
        self.db.add(number)
        self.db.commit()

        response = self.client.get(f"/api/campaigns/{campaign.id}/numbers")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["numbers"][0]["ai_turn_count"], 3)
        self.assertEqual(payload["numbers"][0]["lead_name"], "John Doe")
        self.assertEqual(payload["numbers"][0]["ai_handoff_reason"], "Strong purchase intent")
        self.assertEqual(payload["numbers"][0]["ai_last_user_input"], "Tell me more.")

    def test_ai_runtime_twiml_endpoint_returns_xml(self):
        original_builder = api_router_module.build_ai_runtime_twiml
        api_router_module.build_ai_runtime_twiml = (
            lambda campaign_number_id: '<?xml version="1.0"?><Response><Say>Hello</Say></Response>'
        )
        try:
            response = self.client.get("/api/ai-runtime/twiml/999")
        finally:
            api_router_module.build_ai_runtime_twiml = original_builder

        self.assertEqual(response.status_code, 200)
        self.assertIn("application/xml", response.headers["content-type"])
        self.assertIn("<Say>Hello</Say>", response.text)

    def test_ai_runtime_gather_uses_speech_result_for_ai_campaign(self):
        campaign = Campaign(
            user_id=self.user.id,
            name="Gather AI Campaign",
            caller_id_id=self.caller_id.id,
            country_id=self.country.id,
            ai_agent_id=self.ai_agent.id,
            audio_id=None,
            campaign_mode=CampaignMode.AI_AGENT,
            voice_provider=VoiceProvider.SIGNALWIRE,
            press_1_to_talk_with_agent=False,
            max_concurrent_calls=1,
            status=CampaignStatus.RUNNING,
            total_numbers=1,
        )
        self.db.add(campaign)
        self.db.flush()
        number = CampaignNumber(
            campaign_id=campaign.id,
            phone_number="+15551234567",
            status=CallStatus.IN_PROGRESS,
        )
        self.db.add(number)
        self.db.commit()

        original_builder = api_router_module.build_ai_runtime_followup_twiml
        captured = {}

        def fake_builder(campaign_number_id, user_input):
            captured["campaign_number_id"] = campaign_number_id
            captured["user_input"] = user_input
            return '<?xml version="1.0"?><Response><Gather/></Response>'

        api_router_module.build_ai_runtime_followup_twiml = fake_builder
        try:
            response = self.client.post(
                f"/api/ai-runtime/twiml/{number.id}/gather",
                data={"SpeechResult": "Yes, tell me more"},
            )
        finally:
            api_router_module.build_ai_runtime_followup_twiml = original_builder

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["campaign_number_id"], number.id)
        self.assertEqual(captured["user_input"], "Yes, tell me more")
        self.assertIn("<Gather/>", response.text)

    def test_ai_runtime_audio_endpoint_returns_audio_bytes(self):
        original_getter = api_router_module.get_ai_runtime_audio
        api_router_module.get_ai_runtime_audio = (
            lambda campaign_number_id, audio_token: b"mp3-bytes"
            if campaign_number_id == 55 and audio_token == "token"
            else None
        )
        try:
            response = self.client.get("/api/ai-runtime/audio/55/token")
        finally:
            api_router_module.get_ai_runtime_audio = original_getter

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"mp3-bytes")
        self.assertIn("audio/mpeg", response.headers["content-type"])

    def test_ai_realtime_session_endpoint_requires_edge_secret(self):
        original_secret = api_router_module.settings.AI_REALTIME_EDGE_SECRET
        api_router_module.settings.AI_REALTIME_EDGE_SECRET = "edge-secret"
        try:
            response = self.client.get("/api/ai-runtime/realtime/session/55")
        finally:
            api_router_module.settings.AI_REALTIME_EDGE_SECRET = original_secret

        self.assertEqual(response.status_code, 401)

    def test_ai_realtime_session_endpoint_returns_config_for_edge_worker(self):
        original_secret = api_router_module.settings.AI_REALTIME_EDGE_SECRET
        original_builder = api_router_module.build_ai_realtime_session_config
        api_router_module.settings.AI_REALTIME_EDGE_SECRET = "edge-secret"
        api_router_module.build_ai_realtime_session_config = lambda campaign_number_id: {
            "campaign_number_id": campaign_number_id,
            "provider": "signalwire",
            "model": "gpt-realtime",
        }
        try:
            response = self.client.get(
                "/api/ai-runtime/realtime/session/55",
                headers={"Authorization": "Bearer edge-secret"},
            )
        finally:
            api_router_module.settings.AI_REALTIME_EDGE_SECRET = original_secret
            api_router_module.build_ai_realtime_session_config = original_builder

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["campaign_number_id"], 55)
        self.assertEqual(response.json()["model"], "gpt-realtime")

    def test_ai_realtime_event_endpoint_records_worker_event(self):
        original_secret = api_router_module.settings.AI_REALTIME_EDGE_SECRET
        original_recorder = api_router_module.record_ai_realtime_event
        captured = {}
        api_router_module.settings.AI_REALTIME_EDGE_SECRET = "edge-secret"

        def fake_recorder(campaign_number_id, payload):
            captured["campaign_number_id"] = campaign_number_id
            captured["payload"] = payload

        api_router_module.record_ai_realtime_event = fake_recorder
        try:
            response = self.client.post(
                "/api/ai-runtime/realtime/event/55",
                json={"event": "openai_socket_close", "code": 1000},
                headers={"Authorization": "Bearer edge-secret"},
            )
        finally:
            api_router_module.settings.AI_REALTIME_EDGE_SECRET = original_secret
            api_router_module.record_ai_realtime_event = original_recorder

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["campaign_number_id"], 55)
        self.assertEqual(captured["payload"]["event"], "openai_socket_close")

    def test_ai_realtime_session_config_can_rebuild_without_tmp_session(self):
        campaign = Campaign(
            user_id=self.user.id,
            name="Realtime AI Campaign",
            caller_id_id=self.caller_id.id,
            country_id=self.country.id,
            ai_agent_id=self.ai_agent.id,
            audio_id=None,
            campaign_mode=CampaignMode.AI_AGENT,
            voice_provider=VoiceProvider.SIGNALWIRE,
            press_1_to_talk_with_agent=False,
            max_concurrent_calls=1,
            status=CampaignStatus.RUNNING,
            total_numbers=1,
        )
        self.db.add(campaign)
        self.db.flush()
        number = CampaignNumber(
            campaign_id=campaign.id,
            phone_number="+15551234567",
            lead_name="John Doe",
            status=CallStatus.IN_PROGRESS,
        )
        self.db.add(number)
        self.db.commit()

        service = AICallRuntimeService(
            self.db,
            self.user.id,
            provider=VoiceProvider.SIGNALWIRE.value,
        )
        payload = service.build_realtime_session_config(number.id)

        self.assertEqual(payload["campaign_number_id"], number.id)
        self.assertEqual(payload["provider"], VoiceProvider.SIGNALWIRE.value)
        self.assertEqual(payload["from_number"], self.caller_id.phone_number)
        self.assertEqual(payload["to_number"], "+15551234567")
        self.assertIn("John Doe", payload["instructions"])
        self.assertTrue(payload["openai_api_key"].startswith("sk-test"))


if __name__ == "__main__":
    unittest.main()
