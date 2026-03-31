import os
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.dependencies import require_active_rental
from app.models import (
    AIAgent,
    AIAgentRuntimeProvider,
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
from app.services.user_elevenlabs_service import upsert_user_elevenlabs_credentials
from app.services.user_openai_service import upsert_user_openai_credentials
from app.services.user_signalwire_service import upsert_user_signalwire_credentials


class AICampaignRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
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

    def tearDown(self):
        campaigns_router_module._is_worker_online = self.original_worker_check
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
                "numbers_text": "+15551234567\n+15559876543",
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
        finally:
            db.close()

    def test_create_ai_agent_campaign_with_csv_header_persists_lead_name(self):
        response = self.client.post(
            "/campaigns/create",
            data={
                "name": "AI Campaign CSV",
                "caller_id_id": str(self.caller_id.id),
                "campaign_mode": CampaignMode.AI_AGENT.value,
                "ai_agent_id": str(self.ai_agent.id),
                "voice_provider": VoiceProvider.SIGNALWIRE.value,
                "max_concurrent_calls": "1",
                "numbers_text": "phone_number,name\n+15551234567,Ana",
            },
            files={},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)

        db = self.SessionLocal()
        try:
            campaign = db.query(Campaign).filter(Campaign.name == "AI Campaign CSV").first()
            self.assertIsNotNone(campaign)
            number = db.query(CampaignNumber).filter(CampaignNumber.campaign_id == campaign.id).first()
            self.assertEqual(number.lead_name, "Ana")
            self.assertIn('"name":"Ana"', number.lead_variables_json or "")
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

    def test_ai_runtime_elevenlabs_twiml_endpoint_returns_xml(self):
        original_builder = api_router_module.build_elevenlabs_sip_twiml
        api_router_module.build_elevenlabs_sip_twiml = (
            lambda campaign_number_id: '<?xml version="1.0"?><Response><Dial><Sip>sip:test@sip.elevenlabs.io</Sip></Dial></Response>'
        )
        try:
            response = self.client.get("/api/ai-runtime/elevenlabs/twiml/888")
        finally:
            api_router_module.build_elevenlabs_sip_twiml = original_builder

        self.assertEqual(response.status_code, 200)
        self.assertIn("application/xml", response.headers["content-type"])
        self.assertIn("<Sip>sip:test@sip.elevenlabs.io</Sip>", response.text)

    def test_create_ai_agent_campaign_requires_caller_mapping_for_elevenlabs_runtime(self):
        self.ai_agent.runtime_provider = AIAgentRuntimeProvider.ELEVENLABS_AGENT
        self.ai_agent.external_agent_id = "ext_agent_1"
        self.db.commit()

        response = self.client.post(
            "/campaigns/create",
            data={
                "name": "AI Campaign",
                "caller_id_id": str(self.caller_id.id),
                "campaign_mode": CampaignMode.AI_AGENT.value,
                "ai_agent_id": str(self.ai_agent.id),
                "voice_provider": VoiceProvider.SIGNALWIRE.value,
                "max_concurrent_calls": "2",
                "numbers_text": "+15551234567",
            },
            files={},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("missing ElevenLabs Phone Number ID", response.text)

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
        original_media_type = api_router_module.get_ai_runtime_audio_media_type
        api_router_module.get_ai_runtime_audio = (
            lambda campaign_number_id, audio_token: b"mp3-bytes"
            if campaign_number_id == 55 and audio_token == "token"
            else None
        )
        api_router_module.get_ai_runtime_audio_media_type = lambda: "audio/mpeg"
        try:
            response = self.client.get("/api/ai-runtime/audio/55/token")
        finally:
            api_router_module.get_ai_runtime_audio = original_getter
            api_router_module.get_ai_runtime_audio_media_type = original_media_type

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"mp3-bytes")
        self.assertIn("audio/mpeg", response.headers["content-type"])
        self.assertEqual(response.headers["cache-control"], "public, max-age=3600, immutable")


if __name__ == "__main__":
    unittest.main()
