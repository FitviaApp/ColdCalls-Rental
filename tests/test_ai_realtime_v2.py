import unittest

from app.services.ai_agent_service import normalize_ai_agent_realtime_config
from app.services.ai_realtime_event_bus import AIRealtimeEvent


class AIRealtimeV2Tests(unittest.TestCase):
    def test_realtime_agent_config_normalizes_voice_and_model(self):
        model, voice = normalize_ai_agent_realtime_config(
            model="gpt-realtime",
            voice_id="Verse",
        )
        self.assertEqual(model, "gpt-realtime")
        self.assertEqual(voice, "verse")

    def test_realtime_agent_config_rejects_non_realtime_model(self):
        with self.assertRaises(ValueError):
            normalize_ai_agent_realtime_config(
                model="gpt-4o-mini",
                voice_id="alloy",
            )

    def test_realtime_event_serialization_roundtrip(self):
        event = AIRealtimeEvent(
            event="tool.transfer_call",
            campaign_number_id=123,
            payload={"reason": "Lead requested a human"},
        )
        decoded = AIRealtimeEvent.from_json(event.to_json())
        self.assertEqual(decoded.event, "tool.transfer_call")
        self.assertEqual(decoded.campaign_number_id, 123)
        self.assertEqual(decoded.payload["reason"], "Lead requested a human")


if __name__ == "__main__":
    unittest.main()

