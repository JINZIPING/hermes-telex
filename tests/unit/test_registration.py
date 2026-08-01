"""T-01 registration + T-13 enforces_own_access_policy + monitor backfill."""

import os

from hermes_telex import adapter as adp
from hermes_telex import monitor
from tests.conftest import StubClient


def _isolate_allow_all(monkeypatch):
    """Register the internal flag with monkeypatch so its teardown restores the
    real environment even though the adapter writes os.environ directly."""
    monkeypatch.setenv(adp.INTERNAL_ALLOW_ALL_ENV, "sentinel")


class FakeCtx:
    def __init__(self):
        self.platform = None
        self.tools = []

    def register_platform(self, **kwargs):
        self.platform = kwargs

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)


def test_register_platform_kwargs():
    ctx = FakeCtx()
    adp.register(ctx)
    p = ctx.platform
    assert p["name"] == "telex"
    assert p["cron_deliver_env_var"] == "TELEX_HOME_CHANNEL"
    assert callable(p["standalone_sender_fn"])
    assert callable(p["env_enablement_fn"])
    assert p["max_message_length"] == adp.MAX_MESSAGE_LENGTH
    # access enforced in-plugin, not via gateway allowlist
    assert "allowed_users_env" not in p
    # tool registered
    assert any(t["name"] == "telex" for t in ctx.tools)


def test_adapter_enforces_own_policy_and_dm_policy(monkeypatch):
    _isolate_allow_all(monkeypatch)

    class Cfg:
        extra = {"api_key": "k", "dm_policy": "pairing"}
    a = adp.TelexAdapter(Cfg())
    assert a.enforces_own_access_policy is True
    assert a._dm_policy == "pairing"


def test_register_advertises_allow_all_env_and_fails_closed(monkeypatch):
    """The gateway defers access control to this plugin via an internal
    allow-all flag. register() must seed it closed so an externally exported
    HERMES_TELEX_ALLOW_ALL cannot grant blanket access."""
    _isolate_allow_all(monkeypatch)
    monkeypatch.setenv(adp.INTERNAL_ALLOW_ALL_ENV, "true")

    ctx = FakeCtx()
    adp.register(ctx)

    assert ctx.platform["allow_all_env"] == adp.INTERNAL_ALLOW_ALL_ENV
    assert os.environ[adp.INTERNAL_ALLOW_ALL_ENV] == "false"


def test_non_pairing_config_takes_over_authorization(monkeypatch):
    """Without pairing the plugin is the sole authority (hermes-seatalk parity),
    so the gateway gate — which refuses to trust group_policy "open" and would
    otherwise deny every channel message — stands down."""
    _isolate_allow_all(monkeypatch)

    class Cfg:
        extra = {"accounts": {"default": {
            "api_key": "k", "dm_policy": "allowlist", "allow_from": ["u"],
            "group_policy": "open", "group_sender_allow_from": ["u"],
            "enabled": True,
        }}}
    adp.TelexAdapter(Cfg())

    assert os.environ[adp.INTERNAL_ALLOW_ALL_ENV] == "true"


def test_pairing_config_keeps_gateway_gate_and_trusts_groups(monkeypatch):
    """dm_policy=pairing needs the gateway to deny an unpaired DM so it can
    issue the pairing code, so allow-all stays off. Group traffic must still be
    advertised as an allowlist or the gateway would deny channels outright."""
    _isolate_allow_all(monkeypatch)

    class Cfg:
        extra = {"accounts": {"default": {
            "api_key": "k", "dm_policy": "pairing",
            "group_policy": "open", "group_sender_allow_from": ["u"],
            "enabled": True,
        }}}
    a = adp.TelexAdapter(Cfg())

    assert os.environ[adp.INTERNAL_ALLOW_ALL_ENV] == "false"
    assert a._dm_policy == "pairing"
    assert a._group_policy == "allowlist"


def test_ungated_open_group_is_not_advertised_as_allowlist(monkeypatch):
    """A genuinely ungated open group must not claim to be an allowlist — that
    would defeat the gateway's fail-open guard."""
    _isolate_allow_all(monkeypatch)

    class Cfg:
        extra = {"accounts": {"default": {
            "api_key": "k", "dm_policy": "pairing",
            "group_policy": "open", "enabled": True,
        }}}
    a = adp.TelexAdapter(Cfg())

    assert a._group_policy == "open"


def test_check_requirements_no_env(monkeypatch):
    # check_fn is a dependency check only; must NOT require TELEX_API_KEY
    # (config may live in config.yaml). aiohttp is installed in the test venv.
    monkeypatch.delenv("TELEX_API_KEY", raising=False)
    assert adp.check_telex_requirements() is True


def test_adapter_constructs_with_accounts_default():
    # Exact Voyager config.yaml shape must build a runtime + validate connected.
    class Cfg:
        extra = {"accounts": {"default": {
            "api_key": "4eb678bda6771", "base_url": "http://192.168.100.1:8000",
            "bot_id": "38e2206954dee62f", "dm_policy": "allowlist",
            "allow_from": ["yuy@sea.com"], "group_policy": "open",
            "group_require_mention": False, "group_sender_allow_from": ["yuy@sea.com"],
            "enabled": True,
        }}}
    a = adp.TelexAdapter(Cfg())
    assert len(a._runtimes) == 1 and "default" in a._runtimes
    assert a._dm_policy == "allowlist"
    assert adp._is_telex_connected(Cfg()) is True


def test_env_enablement_flat_and_home(monkeypatch):
    monkeypatch.setenv("TELEX_API_KEY", "envk")
    monkeypatch.setenv("TELEX_HOME_CHANNEL", "0a1b2c3d4e5f6071")
    result = adp._telex_env_enablement()
    # flat fields (merged into extra by the core hook), plus home_channel key
    assert result["api_key"] == "envk"
    assert result["home_channel"]["chat_id"] == "0a1b2c3d4e5f6071"


def test_env_enablement_home_channel_without_api_key(monkeypatch):
    # Regression: /sethome writes TELEX_HOME_CHANNEL to .env while api_key lives
    # in config.yaml. The home channel MUST still be surfaced (not gated on api_key).
    monkeypatch.delenv("TELEX_API_KEY", raising=False)
    monkeypatch.setenv("TELEX_HOME_CHANNEL", "b7cbc72a481784b0")
    result = adp._telex_env_enablement()
    assert result is not None
    assert result.get("home_channel", {}).get("chat_id") == "b7cbc72a481784b0"
    assert "api_key" not in result  # nothing from env when only HOME_CHANNEL is set


async def test_monitor_repair_window_dispatches_settles_and_marks_read():
    from hermes_telex.monitor import TelexSyncDriver

    c = StubClient()
    # Server says read up to seq 2, conversation is at seq 4: 3 and 4 are the gap.
    c.seed_conversation("conv", 2, 4)
    c.messages_by_conv["conv"] = [
        {"id": "m3", "conversation_id": "conv", "seq": 3, "status": 0, "flags": 0,
         "sender_id": "u", "data": {"blocks": []}},
        {"id": "m4", "conversation_id": "conv", "seq": 4, "status": 0, "flags": 0,
         "sender_id": "u", "data": {"blocks": []}},
    ]
    seen = []

    async def on_message(m):
        seen.append(m["seq"])

    driver = TelexSyncDriver(c, on_message, account_id="test")
    lag_remains = await driver._repair_window("conv")

    assert seen == [3, 4]                       # gap dispatched in order
    assert c.is_disposed("conv", 3) and c.is_disposed("conv", 4)
    assert c.get_cursor("conv") == 4            # contiguous watermark marked read
    assert lag_remains is False
    assert ("/mark-read", {"conversation_id": "conv", "read_seq": 4}) in c.posts


async def test_monitor_repair_skips_already_disposed():
    from hermes_telex.monitor import TelexSyncDriver

    c = StubClient()
    c.seed_conversation("conv", 2, 3)
    c.settle("conv", 3)                         # already handled, not yet marked
    c.messages_by_conv["conv"] = [
        {"id": "m3", "conversation_id": "conv", "seq": 3, "status": 0, "flags": 0,
         "sender_id": "u", "data": {"blocks": []}},
    ]
    seen = []

    async def on_message(m):
        seen.append(m["seq"])

    driver = TelexSyncDriver(c, on_message, account_id="test")
    await driver._repair_window("conv")
    assert seen == []                           # no re-dispatch


async def test_standalone_send(tmp_path, monkeypatch):
    # standalone_sender_fn uses a real client; patch send to avoid network
    sent = {}

    async def fake_send(client, **kwargs):
        sent.update(kwargs)
        return {"id": "mid1"}

    monkeypatch.setattr(adp.sendmod, "send_telex_message", fake_send)

    class PCfg:
        extra = {"api_key": "k", "base_url": "https://t"}
    res = await adp._telex_standalone_send(PCfg(), "0a1b2c3d4e5f6071", "hello")
    assert res == {"success": True, "message_id": "mid1"}
    assert sent["conversation_id"] == "0a1b2c3d4e5f6071"


async def test_connect_accepts_is_reconnect(monkeypatch):
    # Regression: newer hermes builds call adapter.connect(is_reconnect=...);
    # a strict connect(self) signature made every connect/reconnect fail with
    # "unexpected keyword argument 'is_reconnect'" and telex never came up.
    import asyncio

    started = []

    async def fake_monitor(client, on_message, stop_event, *, account_id="default"):
        started.append(account_id)
        # Behave like the real monitor: run until stopped, absorb cancellation.
        try:
            await stop_event.wait()
        except asyncio.CancelledError:
            pass

    monkeypatch.setattr(adp, "run_monitor", fake_monitor)

    class Cfg:
        extra = {"api_key": "k"}

    a = adp.TelexAdapter(Cfg())
    assert await a.connect(is_reconnect=False) is True
    await asyncio.sleep(0)   # let the monitor task actually start
    # Reconnect must be re-entrant: the old monitor is stopped, not stacked.
    assert await a.connect(is_reconnect=True) is True
    await asyncio.sleep(0)
    await a.disconnect()
    assert started.count("default") == 2
