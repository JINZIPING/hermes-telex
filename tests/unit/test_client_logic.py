"""T-02/T-04/T-05 client pure logic: self-echo, watermark, dedup, mention."""

from hermes_telex.client import TelexClient


def _c():
    return TelexClient("k", "https://t", bot_id="bot1")


def test_self_echo_via_bot_id_and_sent_cache():
    c = _c()
    assert c.is_own_message({"id": "x", "sender_id": "bot1"}) is True
    assert c.is_own_message({"id": "y", "sender_id": "other"}) is False
    c.record_sent({"id": "z", "sender_id": "bot2"})  # learned self id wins
    assert c.self_id == "bot2"
    assert c.is_own_message({"id": "z", "sender_id": "someoneelse"}) is True  # sent-id cache


def test_dedup_by_settle():
    # Dedup is now cursor/settled based (the monitor gates on is_disposed).
    c = _c()
    c.seed_conversation("conv", 0, 0)
    assert c.is_disposed("conv", 5) is False
    c.settle("conv", 5)
    assert c.is_disposed("conv", 5) is True


def test_unseeded_conversation_is_disposed():
    # Nothing is replayed for a conversation that was never seeded from the
    # server read cursor — otherwise a restart would re-dispatch history.
    c = _c()
    assert c.is_disposed("never-seeded", 10) is True


def test_seed_from_server_read_cursor():
    c = _c()
    c.seed_conversation("conv", cursor=7, max_seen=9)
    assert c.get_cursor("conv") == 7
    assert c.is_disposed("conv", 7) is True    # at/below the read cursor
    assert c.is_disposed("conv", 8) is False   # unread
    assert c.is_lagging("conv") is True        # cursor 7 < max_seen 9


def test_cursor_advance_prunes_settled_and_poison():
    c = _c()
    c.seed_conversation("conv", 0, 0)
    c.settle("conv", 3)
    c.bump_poison("conv", 4)
    c.update_cursor("conv", 5)                 # cursor passes both
    assert c.get_cursor("conv") == 5
    assert c.is_disposed("conv", 3) is True    # below cursor
    assert c.poison_count("conv", 4) == 0      # pruned
    assert c.is_disposed("conv", 6) is False


def test_poison_counts_up():
    c = _c()
    c.seed_conversation("conv", 0, 0)
    assert c.bump_poison("conv", 9) == 1
    assert c.bump_poison("conv", 9) == 2
    assert c.poison_count("conv", 9) == 2


def test_self_mentioned():
    c = _c()
    assert c.is_self_mentioned({"data": {"mention_all": True}}) is True
    assert c.is_self_mentioned({"data": {"mention_ids": ["bot1"]}}) is True
    assert c.is_self_mentioned({"data": {"mention_ids": ["x"]}}) is False


def test_turn_seq():
    c = _c()
    c.note_turn_seq("conv", 3)
    c.note_turn_seq("conv", 2)
    assert c.get_last_turn_seq("conv") == 3


def test_session_recreated_per_event_loop():
    # Regression: tool registry runs handlers in fresh event loops; a session
    # bound to another loop must not be reused ("Timeout context manager
    # should be used inside a task").
    import asyncio
    import warnings

    c = _c()

    async def grab():
        return c._get_session()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # unclosed-session noise from loop 1
        s1 = asyncio.run(grab())
        s2 = asyncio.run(grab())
    assert s1 is not s2
