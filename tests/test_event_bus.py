import unittest

from core.event_bus import EVENT_NAMES, Event, EventBus


class EventBusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.bus = EventBus()

    def test_subscribe_and_publish(self):
        received = []
        self.bus.subscribe("UserMessageReceived", lambda e: received.append(e.payload.get("text")))
        delivered = self.bus.publish("UserMessageReceived", text="你好")
        self.assertEqual(delivered, 1)
        self.assertEqual(received, ["你好"])

    def test_multiple_subscribers_all_receive(self):
        hits = {"a": 0, "b": 0}
        self.bus.subscribe("EmotionChanged", lambda e: hits.__setitem__("a", hits["a"] + 1))
        self.bus.subscribe("EmotionChanged", lambda e: hits.__setitem__("b", hits["b"] + 1))
        delivered = self.bus.publish("EmotionChanged", mood=0.5)
        self.assertEqual(delivered, 2)
        self.assertEqual(hits, {"a": 1, "b": 1})
        self.assertEqual(self.bus.subscriber_count("EmotionChanged"), 2)

    def test_unsubscribe_stops_delivery(self):
        received = []
        handler = lambda e: received.append(e.id)  # noqa: E731
        unsubscribe = self.bus.subscribe("MemoryCreated", handler)
        self.bus.publish("MemoryCreated")
        self.assertTrue(unsubscribe())
        self.bus.publish("MemoryCreated")
        self.assertEqual(len(received), 1)
        self.assertFalse(unsubscribe())

    def test_unsubscribe_by_method(self):
        handler = lambda e: None  # noqa: E731
        self.bus.subscribe("TopicUpdated", handler)
        self.assertTrue(self.bus.unsubscribe("TopicUpdated", handler))
        self.assertFalse(self.bus.unsubscribe("TopicUpdated", handler))

    def test_handler_error_does_not_break_others(self):
        received = []

        def boom(_event):
            raise RuntimeError("订阅者炸了")

        self.bus.subscribe("RelationshipChanged", boom)
        self.bus.subscribe("RelationshipChanged", lambda e: received.append(1))
        delivered = self.bus.publish("RelationshipChanged")
        self.assertEqual(delivered, 1)
        self.assertEqual(received, [1])
        self.assertEqual(self.bus.stats()["failed"], 1)

    def test_event_object_payload_merge(self):
        received = []
        self.bus.subscribe("ProactiveTriggered", lambda e: received.append(e.payload))
        self.bus.publish(Event(name="ProactiveTriggered", payload={"score": 0.8}), extra="x")
        self.assertEqual(received[0], {"score": 0.8, "extra": "x"})

    def test_invalid_event_name_rejected(self):
        with self.assertRaises(ValueError):
            self.bus.subscribe("NotAnEvent", lambda e: None)
        with self.assertRaises(ValueError):
            self.bus.publish("NotAnEvent")

    def test_event_names_match_spec(self):
        self.assertEqual(
            set(EVENT_NAMES),
            {
                "UserMessageReceived", "TopicUpdated", "MemoryCreated", "MemoryRecalled",
                "EmotionChanged", "RelationshipChanged", "ConversationEnded",
                "ProactiveTriggered", "UserReturned",
            },
        )

    def test_clear(self):
        self.bus.subscribe("UserReturned", lambda e: None)
        self.bus.clear("UserReturned")
        self.assertEqual(self.bus.subscriber_count("UserReturned"), 0)


if __name__ == "__main__":
    unittest.main()
