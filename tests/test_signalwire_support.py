import unittest
from types import SimpleNamespace

from app.models import CallStatus, CampaignMode, VoiceProvider
from app.routers.ai_agents import _agent_form_data
from app.routers.campaigns import (
    _create_form_data,
    _campaign_form_validation_error,
    _campaign_resource_validation_error,
    _campaign_start_validation_error,
    _parse_campaign_numbers,
)
from app.services.ai_call_runtime_service import (
    AI_RUNTIME_DIR,
    AICallRuntimeService,
    cleanup_ai_runtime_artifacts,
    prune_stale_ai_runtime_artifacts,
)
from app.services.campaign_worker import (
    CampaignWorker,
    SIGNALWIRE_MAX_START_INTERVAL_SECONDS,
    SIGNALWIRE_MIN_START_INTERVAL_SECONDS,
    TWILIO_MIN_START_INTERVAL_SECONDS,
)
from app.services.user_signalwire_service import _normalize_space_url
from app.services.signalwire_service import SignalWireService
from app.services.user_voice_provider_service import (
    has_user_ai_runtime_credentials,
    provider_supports_press_1,
    supported_voice_providers,
)


class SignalWireSupportTests(unittest.TestCase):
    def test_supported_providers_include_signalwire(self):
        providers = supported_voice_providers()
        self.assertIn(VoiceProvider.SIGNALWIRE.value, providers)

    def test_press_1_support_includes_signalwire(self):
        self.assertTrue(provider_supports_press_1(VoiceProvider.SIGNALWIRE.value))
        self.assertTrue(provider_supports_press_1(VoiceProvider.TWILIO.value))
        self.assertTrue(provider_supports_press_1(VoiceProvider.VOXIMPLANT.value))
        self.assertFalse(provider_supports_press_1(VoiceProvider.TELNYX.value))
        self.assertFalse(provider_supports_press_1(VoiceProvider.VONAGE.value))

    def test_signalwire_space_url_normalization(self):
        self.assertEqual(
            _normalize_space_url("https://example.signalwire.com/"),
            "example.signalwire.com",
        )
        self.assertEqual(
            _normalize_space_url("http://example.signalwire.com"),
            "example.signalwire.com",
        )
        self.assertEqual(
            _normalize_space_url("example.signalwire.com/"),
            "example.signalwire.com",
        )

    def test_worker_status_mapping_keeps_expected_behavior(self):
        worker = CampaignWorker(db=None)  # type: ignore[arg-type]
        self.assertEqual(worker._map_status("completed"), CallStatus.COMPLETED)
        self.assertEqual(worker._map_status("no-answer"), CallStatus.NO_ANSWER)
        self.assertEqual(worker._map_status("busy"), CallStatus.BUSY)
        self.assertEqual(worker._map_status("cancelled"), CallStatus.CANCELLED)
        self.assertEqual(
            worker._map_status("unknown-status", default=CallStatus.FAILED),
            CallStatus.FAILED,
        )

    def test_signalwire_rate_limiter_uses_random_interval_between_3_and_5_seconds(self):
        worker = CampaignWorker(db=None)  # type: ignore[arg-type]
        limiter = worker._build_start_rate_limiter(VoiceProvider.SIGNALWIRE.value)

        self.assertIsNotNone(limiter)

        samples = [limiter._current_interval_seconds() for _ in range(25)]  # type: ignore[union-attr]

        self.assertTrue(
            all(
                SIGNALWIRE_MIN_START_INTERVAL_SECONDS <= sample <= SIGNALWIRE_MAX_START_INTERVAL_SECONDS
                for sample in samples
            )
        )
        self.assertGreater(len({round(sample, 3) for sample in samples}), 1)

    def test_twilio_rate_limiter_keeps_fixed_interval(self):
        worker = CampaignWorker(db=None)  # type: ignore[arg-type]
        limiter = worker._build_start_rate_limiter(VoiceProvider.TWILIO.value)

        self.assertIsNotNone(limiter)
        self.assertEqual(limiter._current_interval_seconds(), TWILIO_MIN_START_INTERVAL_SECONDS)  # type: ignore[union-attr]

    def test_ai_runtime_credentials_accept_twilio_or_signalwire(self):
        import app.services.user_voice_provider_service as provider_service

        originals = {
            "has_user_signalwire_credentials": provider_service.has_user_signalwire_credentials,
            "has_user_twilio_credentials": provider_service.has_user_twilio_credentials,
            "has_user_openai_credentials": provider_service.has_user_openai_credentials,
            "has_user_elevenlabs_credentials": provider_service.has_user_elevenlabs_credentials,
        }
        try:
            provider_service.has_user_openai_credentials = lambda db, user_id: True
            provider_service.has_user_elevenlabs_credentials = lambda db, user_id: True
            provider_service.has_user_signalwire_credentials = lambda db, user_id: True
            provider_service.has_user_twilio_credentials = lambda db, user_id: False
            self.assertTrue(has_user_ai_runtime_credentials(None, 1))
            self.assertTrue(has_user_ai_runtime_credentials(None, 1, VoiceProvider.SIGNALWIRE.value))
            self.assertFalse(has_user_ai_runtime_credentials(None, 1, VoiceProvider.TWILIO.value))

            provider_service.has_user_signalwire_credentials = lambda db, user_id: False
            provider_service.has_user_twilio_credentials = lambda db, user_id: True
            self.assertTrue(has_user_ai_runtime_credentials(None, 1))
            self.assertTrue(has_user_ai_runtime_credentials(None, 1, VoiceProvider.TWILIO.value))

            provider_service.has_user_elevenlabs_credentials = lambda db, user_id: False
            self.assertFalse(has_user_ai_runtime_credentials(None, 1))
            self.assertFalse(has_user_ai_runtime_credentials(None, 1, VoiceProvider.TWILIO.value))
        finally:
            for name, value in originals.items():
                setattr(provider_service, name, value)

    def test_campaign_mode_enum_values(self):
        self.assertEqual(CampaignMode.AUDIO.value, "audio")
        self.assertEqual(CampaignMode.AI_AGENT.value, "ai_agent")

    def test_signalwire_answer_url_skips_inline_twiml_and_machine_detection(self):
        import app.services.signalwire_service as signalwire_module

        captured = {}

        class DummyResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"sid": "CA123", "status": "queued"}

        class DummyClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def post(self, url, data=None, headers=None):
                captured["url"] = url
                captured["data"] = dict(data or {})
                captured["headers"] = dict(headers or {})
                return DummyResponse()

        original_client = signalwire_module.httpx.Client
        signalwire_module.httpx.Client = DummyClient
        try:
            service = SignalWireService("project", "token", "space.signalwire.com")
            result = service.make_call(
                to_number="+15551234567",
                from_number="+15557654321",
                audio_url=None,
                transfer_number="+15550001111",
                answer_url="https://example.com/api/ai-runtime/twiml/1",
                enable_machine_detection=False,
            )
        finally:
            signalwire_module.httpx.Client = original_client

        self.assertEqual(result["call_sid"], "CA123")
        self.assertEqual(captured["data"]["Url"], "https://example.com/api/ai-runtime/twiml/1")
        self.assertNotIn("Twiml", captured["data"])
        self.assertNotIn("MachineDetection", captured["data"])

    def test_ai_runtime_cleanup_removes_session_and_audio_files(self):
        campaign_number_id = 4242
        session_path = AI_RUNTIME_DIR / f"{campaign_number_id}.json"
        audio_path = AI_RUNTIME_DIR / f"{campaign_number_id}-demo.mp3"

        session_path.write_text("{}")
        audio_path.write_bytes(b"demo")

        cleanup_ai_runtime_artifacts(campaign_number_id)

        self.assertFalse(session_path.exists())
        self.assertFalse(audio_path.exists())

    def test_ai_runtime_prune_removes_only_stale_artifacts(self):
        import os
        import time

        stale_session = AI_RUNTIME_DIR / "8001.json"
        stale_audio = AI_RUNTIME_DIR / "8001-token.mp3"
        fresh_session = AI_RUNTIME_DIR / "8002.json"

        stale_session.write_text("{}")
        stale_audio.write_bytes(b"demo")
        fresh_session.write_text("{}")

        stale_timestamp = time.time() - 120
        os.utime(stale_session, (stale_timestamp, stale_timestamp))
        os.utime(stale_audio, (stale_timestamp, stale_timestamp))

        removed = prune_stale_ai_runtime_artifacts(max_age_seconds=60)

        self.assertEqual(removed, 2)
        self.assertFalse(stale_session.exists())
        self.assertFalse(stale_audio.exists())
        self.assertTrue(fresh_session.exists())

        fresh_session.unlink(missing_ok=True)

    def test_campaign_form_validation_rejects_non_ai_capable_provider_for_ai_mode(self):
        error = _campaign_form_validation_error(
            voice_provider=VoiceProvider.TELNYX.value,
            campaign_mode=CampaignMode.AI_AGENT.value,
            press_1_to_talk_with_agent=False,
            max_concurrent_calls=1,
            provider_configured=True,
            ai_runtime_configured=True,
        )
        self.assertEqual(error, "AI agent campaigns support only Twilio or SignalWire.")

    def test_campaign_form_validation_accepts_twilio_for_ai_mode(self):
        error = _campaign_form_validation_error(
            voice_provider=VoiceProvider.TWILIO.value,
            campaign_mode=CampaignMode.AI_AGENT.value,
            press_1_to_talk_with_agent=False,
            max_concurrent_calls=1,
            provider_configured=True,
            ai_runtime_configured=True,
        )
        self.assertIsNone(error)

    def test_campaign_form_validation_rejects_press_1_for_ai_mode(self):
        error = _campaign_form_validation_error(
            voice_provider=VoiceProvider.SIGNALWIRE.value,
            campaign_mode=CampaignMode.AI_AGENT.value,
            press_1_to_talk_with_agent=True,
            max_concurrent_calls=1,
            provider_configured=True,
            ai_runtime_configured=True,
        )
        self.assertEqual(error, "Press 1 flow is not available for AI agent campaigns.")

    def test_campaign_resource_validation_requires_active_ai_agent(self):
        caller_id = SimpleNamespace(vox_verification_status="verified")
        error = _campaign_resource_validation_error(
            campaign_mode=CampaignMode.AI_AGENT.value,
            voice_provider=VoiceProvider.SIGNALWIRE.value,
            caller_id=caller_id,
            audio=None,
            ai_agent=None,
            selected_audio_id=None,
            selected_ai_agent_id=10,
        )
        self.assertEqual(error, "Select an active AI agent for AI agent campaigns.")

    def test_campaign_start_validation_requires_ai_runtime_credentials(self):
        user = SimpleNamespace(id=1, transfer_number="+15550001111")
        caller_id = SimpleNamespace(
            user_id=1,
            vox_verification_status="verified",
        )
        ai_agent = SimpleNamespace(user_id=1, is_active=True)
        campaign = SimpleNamespace(
            voice_provider=VoiceProvider.SIGNALWIRE,
            campaign_mode=CampaignMode.AI_AGENT,
            ai_agent_id=5,
            ai_agent=ai_agent,
            press_1_to_talk_with_agent=False,
            caller_id=caller_id,
            audio_id=None,
            audio=None,
        )
        error = _campaign_start_validation_error(
            campaign=campaign,
            user=user,
            provider_configured=True,
            ai_runtime_configured=False,
        )
        self.assertEqual(
            error,
            "Please configure SignalWire, OpenAI, and ElevenLabs credentials in Settings first",
        )

    def test_campaign_start_validation_accepts_twilio_for_ai_mode(self):
        user = SimpleNamespace(id=1, transfer_number="+15550001111")
        caller_id = SimpleNamespace(
            user_id=1,
            vox_verification_status="verified",
        )
        ai_agent = SimpleNamespace(user_id=1, is_active=True)
        campaign = SimpleNamespace(
            voice_provider=VoiceProvider.TWILIO,
            campaign_mode=CampaignMode.AI_AGENT,
            ai_agent_id=5,
            ai_agent=ai_agent,
            press_1_to_talk_with_agent=False,
            caller_id=caller_id,
            audio_id=None,
            audio=None,
        )
        error = _campaign_start_validation_error(
            campaign=campaign,
            user=user,
            provider_configured=True,
            ai_runtime_configured=True,
        )
        self.assertIsNone(error)

    def test_parse_campaign_numbers_accepts_csv_first_column_and_counts_invalid(self):
        leads, invalid_count = _parse_campaign_numbers(
            "+15551234567\n+15557654321,John Doe\ninvalid-number\n\n"
        )
        self.assertEqual([lead.phone_number for lead in leads], ["+15551234567", "+15557654321"])
        self.assertIsNone(leads[0].lead_name)
        self.assertEqual(leads[1].lead_name, "John Doe")
        self.assertEqual(invalid_count, 1)

    def test_parse_campaign_numbers_accepts_optional_phone_name_header(self):
        leads, invalid_count = _parse_campaign_numbers(
            "phone,name\n+15551234567,John Doe\n+15557654321,Maria Silva\n"
        )
        self.assertEqual([lead.phone_number for lead in leads], ["+15551234567", "+15557654321"])
        self.assertEqual([lead.lead_name for lead in leads], ["John Doe", "Maria Silva"])
        self.assertEqual(invalid_count, 0)

    def test_parse_campaign_numbers_keeps_legacy_phone_only_lists(self):
        leads, invalid_count = _parse_campaign_numbers(
            "+15551234567\n+15557654321\n"
        )
        self.assertEqual([lead.phone_number for lead in leads], ["+15551234567", "+15557654321"])
        self.assertEqual([lead.lead_name for lead in leads], [None, None])
        self.assertEqual(invalid_count, 0)

    def test_create_form_data_preserves_expected_fields(self):
        form_data = _create_form_data(
            name="AI Outreach",
            caller_id_id=3,
            audio_id="",
            ai_agent_id="7",
            campaign_mode=CampaignMode.AI_AGENT.value,
            voice_provider=VoiceProvider.SIGNALWIRE.value,
            press_1_to_talk_with_agent=False,
            max_concurrent_calls=4,
            numbers_text="+15551234567",
        )
        self.assertEqual(form_data["name"], "AI Outreach")
        self.assertEqual(form_data["caller_id_id"], "3")
        self.assertEqual(form_data["ai_agent_id"], "7")
        self.assertEqual(form_data["campaign_mode"], "ai_agent")
        self.assertEqual(form_data["voice_provider"], "signalwire")
        self.assertEqual(form_data["max_concurrent_calls"], 4)
        self.assertEqual(form_data["numbers_text"], "+15551234567")

    def test_ai_agent_form_data_preserves_expected_fields(self):
        form_data = _agent_form_data(
            name="Qualifier",
            system_prompt="Be concise.",
            voice_id="voice_123",
            model="gpt-4o-mini",
            temperature=0.4,
            language="en",
            handoff_description="Transfer on strong interest.",
            is_active=False,
        )
        self.assertEqual(form_data["name"], "Qualifier")
        self.assertEqual(form_data["voice_id"], "voice_123")
        self.assertEqual(form_data["temperature"], 0.4)
        self.assertEqual(form_data["handoff_description"], "Transfer on strong interest.")
        self.assertFalse(form_data["is_active"])

    def test_ai_runtime_post_with_retries_succeeds_after_transient_failure(self):
        import app.services.ai_call_runtime_service as runtime_module

        original_client = runtime_module._HTTP_CLIENT
        original_retries = runtime_module.settings.AI_HTTP_MAX_RETRIES
        original_backoff = runtime_module.settings.AI_HTTP_RETRY_BACKOFF_SECONDS

        attempts = {"count": 0}

        class DummyResponse:
            def raise_for_status(self):
                return None

        class DummyClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def post(self, url, headers=None, json=None, timeout=None):
                attempts["count"] += 1
                if attempts["count"] == 1:
                    raise runtime_module.httpx.TimeoutException("temporary timeout")
                return DummyResponse()

        runtime_module._HTTP_CLIENT = DummyClient()
        runtime_module.settings.AI_HTTP_MAX_RETRIES = 2
        runtime_module.settings.AI_HTTP_RETRY_BACKOFF_SECONDS = 0
        try:
            service = AICallRuntimeService.__new__(AICallRuntimeService)
            response = service._post_with_retries(
                provider_name="OpenAI",
                url="https://example.com",
                headers={},
                json_payload={},
                timeout_seconds=1.0,
            )
        finally:
            runtime_module._HTTP_CLIENT = original_client
            runtime_module.settings.AI_HTTP_MAX_RETRIES = original_retries
            runtime_module.settings.AI_HTTP_RETRY_BACKOFF_SECONDS = original_backoff

        self.assertIsNotNone(response)
        self.assertEqual(attempts["count"], 2)

    def test_ai_runtime_sanitizes_long_assistant_text(self):
        import app.services.ai_call_runtime_service as runtime_module

        original_max_chars = runtime_module.MAX_ASSISTANT_TEXT_CHARS
        runtime_module.MAX_ASSISTANT_TEXT_CHARS = 60
        try:
            service = AICallRuntimeService.__new__(AICallRuntimeService)
            sanitized = service._sanitize_assistant_text(
                "This is a long response that keeps going without stopping and should be shortened for voice delivery so it sounds more natural."
            )
        finally:
            runtime_module.MAX_ASSISTANT_TEXT_CHARS = original_max_chars

        self.assertLessEqual(len(sanitized), 63)
        self.assertTrue(sanitized.endswith("...") or sanitized.endswith("."))

    def test_ai_runtime_reprompts_on_empty_input(self):
        import app.services.ai_call_runtime_service as runtime_module

        service = AICallRuntimeService.__new__(AICallRuntimeService)
        captured = {}
        session = {"no_input_turns": 0, "history": [], "campaign_number_id": 9}
        original_update = runtime_module.update_campaign_number_ai_observability

        service._read_session = lambda campaign_number_id: session
        service._create_assistant_turn = lambda payload, assistant_text, should_transfer, **kwargs: {
            "assistant_text": assistant_text,
            "should_transfer": should_transfer,
            "audio_token": "token",
        }
        service._write_session = lambda campaign_number_id, payload: captured.update(payload)
        service._twiml_for_turn = lambda campaign_number_id, payload, turn: turn["assistant_text"]
        runtime_module.update_campaign_number_ai_observability = lambda *args, **kwargs: None

        try:
            result = service.build_followup_twiml(9, user_input="")
        finally:
            runtime_module.update_campaign_number_ai_observability = original_update

        self.assertEqual(result, "I didn't catch that. Are you still there?")
        self.assertEqual(captured["no_input_turns"], 1)

    def test_ai_runtime_request_openai_turn_uses_fallback_for_empty_content(self):
        import app.services.ai_call_runtime_service as runtime_module

        service = AICallRuntimeService.__new__(AICallRuntimeService)
        service.openai_api_key = "sk-test"
        service.openai_org_id = ""
        service._post_json_with_retries = lambda **kwargs: {
            "choices": [{"message": {"content": "", "tool_calls": []}}]
        }
        agent = SimpleNamespace(
            system_prompt="Be helpful.",
            handoff_description="Transfer on request.",
            model="gpt-4o-mini",
            temperature=0.3,
        )

        result = service._request_openai_turn(agent, [])

        self.assertEqual(result["assistant_text"], "Hello, this is a quick follow-up call.")
        self.assertFalse(result["should_transfer"])

    def test_ai_runtime_request_openai_turn_extracts_handoff_reason(self):
        service = AICallRuntimeService.__new__(AICallRuntimeService)
        service.openai_api_key = "sk-test"
        service.openai_org_id = ""
        service._post_json_with_retries = lambda **kwargs: {
            "choices": [{
                "message": {
                    "content": "I can connect you now.",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "transfer_call",
                                "arguments": "{\"reason\":\"Strong purchase intent\"}",
                            }
                        }
                    ],
                }
            }]
        }
        agent = SimpleNamespace(
            system_prompt="Be helpful.",
            handoff_description="Transfer on request.",
            model="gpt-4o-mini",
            temperature=0.3,
        )

        result = service._request_openai_turn(agent, [])

        self.assertTrue(result["should_transfer"])
        self.assertEqual(result["handoff_reason"], "Strong purchase intent")

    def test_ai_runtime_request_openai_turn_includes_lead_name_context(self):
        captured = {}
        service = AICallRuntimeService.__new__(AICallRuntimeService)
        service.openai_api_key = "sk-test"
        service.openai_org_id = ""

        def fake_post_json_with_retries(**kwargs):
            captured["payload"] = kwargs["json_payload"]
            return {"choices": [{"message": {"content": "Hi John.", "tool_calls": []}}]}

        service._post_json_with_retries = fake_post_json_with_retries
        agent = SimpleNamespace(
            system_prompt="Be helpful.",
            handoff_description="Transfer on request.",
            model="gpt-4o-mini",
            temperature=0.3,
        )

        result = service._request_openai_turn(agent, [], lead_name="John Doe")

        system_prompt = captured["payload"]["messages"][0]["content"]
        self.assertIn("The lead's name is John Doe.", system_prompt)
        self.assertEqual(result["assistant_text"], "Hi John.")

    def test_ai_runtime_request_openai_turn_omits_empty_lead_name_context(self):
        captured = {}
        service = AICallRuntimeService.__new__(AICallRuntimeService)
        service.openai_api_key = "sk-test"
        service.openai_org_id = ""

        def fake_post_json_with_retries(**kwargs):
            captured["payload"] = kwargs["json_payload"]
            return {"choices": [{"message": {"content": "Hello.", "tool_calls": []}}]}

        service._post_json_with_retries = fake_post_json_with_retries
        agent = SimpleNamespace(
            system_prompt="Be helpful.",
            handoff_description="Transfer on request.",
            model="gpt-4o-mini",
            temperature=0.3,
        )

        service._request_openai_turn(agent, [], lead_name="")

        system_prompt = captured["payload"]["messages"][0]["content"]
        self.assertNotIn("The lead's name is", system_prompt)

    def test_ai_runtime_twiml_uses_low_latency_gather_defaults(self):
        import app.services.ai_call_runtime_service as runtime_module

        original_timeout = runtime_module.settings.AI_GATHER_TIMEOUT_SECONDS
        original_speech_timeout = runtime_module.settings.AI_GATHER_SPEECH_TIMEOUT_SECONDS
        original_pause = runtime_module.settings.AI_GATHER_POST_PLAY_PAUSE_SECONDS

        runtime_module.settings.AI_GATHER_TIMEOUT_SECONDS = 3
        runtime_module.settings.AI_GATHER_SPEECH_TIMEOUT_SECONDS = 1
        runtime_module.settings.AI_GATHER_POST_PLAY_PAUSE_SECONDS = 0
        try:
            service = AICallRuntimeService.__new__(AICallRuntimeService)
            twiml = service._twiml_for_turn(
                55,
                {"from_number": "+15550001111", "transfer_number": "+15550002222"},
                {"audio_token": "token", "should_transfer": False},
            )
        finally:
            runtime_module.settings.AI_GATHER_TIMEOUT_SECONDS = original_timeout
            runtime_module.settings.AI_GATHER_SPEECH_TIMEOUT_SECONDS = original_speech_timeout
            runtime_module.settings.AI_GATHER_POST_PLAY_PAUSE_SECONDS = original_pause

        self.assertIn('speechTimeout="1"', twiml)
        self.assertIn('timeout="3"', twiml)
        self.assertNotIn("<Pause", twiml)

    def test_ai_runtime_twiml_includes_optional_post_play_pause(self):
        import app.services.ai_call_runtime_service as runtime_module

        original_pause = runtime_module.settings.AI_GATHER_POST_PLAY_PAUSE_SECONDS
        runtime_module.settings.AI_GATHER_POST_PLAY_PAUSE_SECONDS = 2
        try:
            service = AICallRuntimeService.__new__(AICallRuntimeService)
            twiml = service._twiml_for_turn(
                55,
                {"from_number": "+15550001111", "transfer_number": "+15550002222"},
                {"audio_token": "token", "should_transfer": False},
            )
        finally:
            runtime_module.settings.AI_GATHER_POST_PLAY_PAUSE_SECONDS = original_pause

        self.assertIn('<Pause length="2"/>', twiml)

    def test_ai_runtime_post_binary_with_retries_retries_on_empty_audio(self):
        import app.services.ai_call_runtime_service as runtime_module

        original_client = runtime_module._HTTP_CLIENT
        original_retries = runtime_module.settings.AI_HTTP_MAX_RETRIES
        original_backoff = runtime_module.settings.AI_HTTP_RETRY_BACKOFF_SECONDS

        attempts = {"count": 0}

        class DummyResponse:
            def __init__(self, content, content_type="audio/mpeg"):
                self.content = content
                self.headers = {"content-type": content_type}
                self.text = ""

            def raise_for_status(self):
                return None

        class DummyClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def post(self, url, headers=None, json=None, timeout=None):
                attempts["count"] += 1
                if attempts["count"] == 1:
                    return DummyResponse(b"")
                return DummyResponse(b"mp3-bytes")

        runtime_module._HTTP_CLIENT = DummyClient()
        runtime_module.settings.AI_HTTP_MAX_RETRIES = 2
        runtime_module.settings.AI_HTTP_RETRY_BACKOFF_SECONDS = 0
        try:
            service = AICallRuntimeService.__new__(AICallRuntimeService)
            audio_bytes = service._post_binary_with_retries(
                provider_name="ElevenLabs",
                url="https://example.com",
                headers={},
                json_payload={},
                timeout_seconds=1.0,
            )
        finally:
            runtime_module._HTTP_CLIENT = original_client
            runtime_module.settings.AI_HTTP_MAX_RETRIES = original_retries
            runtime_module.settings.AI_HTTP_RETRY_BACKOFF_SECONDS = original_backoff

        self.assertEqual(audio_bytes, b"mp3-bytes")
        self.assertEqual(attempts["count"], 2)

    def test_ai_runtime_post_binary_with_retries_raises_for_non_audio_payload(self):
        import app.services.ai_call_runtime_service as runtime_module

        original_client = runtime_module._HTTP_CLIENT
        original_retries = runtime_module.settings.AI_HTTP_MAX_RETRIES
        original_backoff = runtime_module.settings.AI_HTTP_RETRY_BACKOFF_SECONDS

        class DummyResponse:
            def __init__(self):
                self.content = b'{"detail":"voice not found"}'
                self.headers = {"content-type": "application/json"}
                self.text = '{"detail":"voice not found"}'

            def raise_for_status(self):
                return None

        class DummyClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def post(self, url, headers=None, json=None, timeout=None):
                return DummyResponse()

        runtime_module._HTTP_CLIENT = DummyClient()
        runtime_module.settings.AI_HTTP_MAX_RETRIES = 0
        runtime_module.settings.AI_HTTP_RETRY_BACKOFF_SECONDS = 0
        try:
            service = AICallRuntimeService.__new__(AICallRuntimeService)
            with self.assertRaises(RuntimeError) as ctx:
                service._post_binary_with_retries(
                    provider_name="ElevenLabs",
                    url="https://example.com",
                    headers={},
                    json_payload={},
                    timeout_seconds=1.0,
                )
        finally:
            runtime_module._HTTP_CLIENT = original_client
            runtime_module.settings.AI_HTTP_MAX_RETRIES = original_retries
            runtime_module.settings.AI_HTTP_RETRY_BACKOFF_SECONDS = original_backoff

        self.assertIn("application/json", str(ctx.exception))
        self.assertIn("voice not found", str(ctx.exception))

    def test_ai_runtime_policy_error_enforces_duration_and_cost_limits(self):
        import app.services.campaign_worker as worker_module

        original_max_duration = worker_module.settings.AI_MAX_CALL_DURATION_SECONDS
        original_max_cost = worker_module.settings.AI_MAX_CALL_COST_USD
        worker_module.settings.AI_MAX_CALL_DURATION_SECONDS = 30
        worker_module.settings.AI_MAX_CALL_COST_USD = 1.0
        try:
            worker = CampaignWorker(db=None)  # type: ignore[arg-type]
            ai_campaign = SimpleNamespace(campaign_mode=CampaignMode.AI_AGENT)
            audio_campaign = SimpleNamespace(campaign_mode=CampaignMode.AUDIO)

            self.assertIn(
                "max duration policy",
                worker._ai_runtime_policy_error(ai_campaign, final_duration=45, cost=0.5),
            )
            self.assertIn(
                "max cost policy",
                worker._ai_runtime_policy_error(ai_campaign, final_duration=20, cost=1.5),
            )
            self.assertIsNone(
                worker._ai_runtime_policy_error(ai_campaign, final_duration=20, cost=0.5)
            )
            self.assertIsNone(
                worker._ai_runtime_policy_error(audio_campaign, final_duration=999, cost=99)
            )
        finally:
            worker_module.settings.AI_MAX_CALL_DURATION_SECONDS = original_max_duration
            worker_module.settings.AI_MAX_CALL_COST_USD = original_max_cost


if __name__ == "__main__":
    unittest.main()
