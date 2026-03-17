import unittest

from app.models import CallStatus, VoiceProvider
from app.services.campaign_worker import CampaignWorker
from app.services.user_signalwire_service import _normalize_space_url
from app.services.user_voice_provider_service import (
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


if __name__ == "__main__":
    unittest.main()
