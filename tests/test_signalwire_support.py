import unittest

from app.models import CallStatus, VoiceProvider
from app.services.campaign_worker import (
    CampaignWorker,
    SIGNALWIRE_MAX_START_INTERVAL_SECONDS,
    SIGNALWIRE_MIN_START_INTERVAL_SECONDS,
    TWILIO_MIN_START_INTERVAL_SECONDS,
)
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


if __name__ == "__main__":
    unittest.main()
