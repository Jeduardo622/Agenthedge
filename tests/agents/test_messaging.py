from agents.messaging import MessageBus


def test_publish_and_subscribe_with_replay():
    bus = MessageBus(max_history=10)
    received = []

    def handler(envelope):
        received.append(envelope.message.payload["value"])

    # publish before subscriptions to test replay capability
    for value in range(3):
        bus.publish("alpha", {"value": value})

    bus.subscribe(handler, topics=["alpha"], replay_last=2)
    bus.publish("alpha", {"value": 99})

    assert received == [1, 2, 99]
    assert len(bus.history()) == 4


def test_subscription_wildcard():
    bus = MessageBus()
    received = []

    bus.subscribe(lambda env: received.append(env.message.topic), topics=["*"])
    bus.publish("alpha", {"value": 1})
    bus.publish("beta", {"value": 2})

    assert received == ["alpha", "beta"]
