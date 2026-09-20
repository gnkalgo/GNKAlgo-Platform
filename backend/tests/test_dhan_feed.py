import asyncio
import json

import pytest

from app.market.adapters import DhanV2Adapter
from app.market.dhan import (
    DEPTH_LEVEL,
    DISCONNECT,
    FULL,
    OPEN_INTEREST,
    QUOTE,
    TICKER,
    DhanProtocolError,
    DhanFeedSession,
    DhanSubscription,
    decode_dhan_frames,
    dhan_subscription,
    subscription_messages,
)
from app.models import Instrument


def instrument(exchange="NSE", segment="EQ", token="1333"):
    return Instrument(id="instrument-1", exchange=exchange, segment=segment, symbol="SBIN",
                      trading_symbol="SBIN-EQ", name="State Bank of India", instrument_type="EQUITY",
                      broker_tokens={"DHAN": token})


def test_decodes_concatenated_ticker_and_quote_packets():
    ticker = TICKER.pack(2, TICKER.size, 1, 1333, 812.25, 1_700_000_000)
    quote = QUOTE.pack(4, QUOTE.size, 1, 1333, 813.5, 10, 1_700_000_001, 810.0,
                       1_000, 200, 300, 800.0, 790.0, 820.0, 780.0)

    packets = decode_dhan_frames(ticker + quote)

    assert [packet["response_code"] for packet in packets] == [2, 4]
    assert packets[0]["security_id"] == "1333"
    assert packets[1]["LTP"] == pytest.approx(813.5)
    assert packets[1]["volume"] == 1_000


def test_decodes_full_packet_depth_and_normalizes_quote():
    depth = b"".join(DEPTH_LEVEL.pack(100 + level, 200 + level, 2, 3, 812.0 - level, 812.5 + level)
                     for level in range(5))
    packet = FULL.pack(8, FULL.size, 1, 1333, 812.25, 10, 1_700_000_000, 810.0,
                       1_000, 200, 300, 400, 450, 350, 800.0, 790.0, 820.0, 780.0, depth)

    decoded = decode_dhan_frames(packet)[0]
    quote = DhanV2Adapter().normalize(instrument(), decoded, 7)

    assert decoded["depth"]["levels"][0]["bid_quantity"] == 100
    assert decoded["depth"]["levels"][4]["ask"] == pytest.approx(816.5)
    assert quote.ltp == pytest.approx(812.25)
    assert quote.open_interest == 400
    assert quote.mode == "depth"


def test_decodes_disconnect_and_rejects_truncated_packet():
    packet = DISCONNECT.pack(50, DISCONNECT.size, 0, 0, 807)
    assert decode_dhan_frames(packet)[0]["disconnect_code"] == 807
    with pytest.raises(DhanProtocolError, match="exceeds"):
        decode_dhan_frames(packet[:-1])


def test_builds_subscription_messages_in_broker_batches():
    row = instrument()
    subscriptions = [DhanSubscription(str(index), "NSE_EQ", 1, str(index), row) for index in range(205)]

    messages = [json.loads(message) for message in subscription_messages(subscriptions, 17)]
    unsubscribe = json.loads(subscription_messages(subscriptions[:1], 18)[0])

    assert [message["InstrumentCount"] for message in messages] == [100, 100, 5]
    assert all(message["RequestCode"] == 17 for message in messages)
    assert unsubscribe["RequestCode"] == 18


@pytest.mark.parametrize(("exchange", "segment", "expected"), [
    ("NSE", "EQ", "NSE_EQ"),
    ("NFO", "FNO", "NSE_FNO"),
    ("BSE", "EQ", "BSE_EQ"),
    ("MCX", "COMM", "MCX_COMM"),
])
def test_maps_instrument_to_dhan_segment(exchange, segment, expected):
    subscription = dhan_subscription(instrument(exchange, segment))
    assert subscription.exchange_segment == expected


def test_explicit_broker_token_segment_overrides_derivation():
    row = instrument("CUSTOM", "CUSTOM", {"security_id": "42", "exchange_segment": "BSE_FNO"})
    subscription = dhan_subscription(row)
    assert subscription.security_id == "42"
    assert subscription.exchange_code == 8


def test_rejects_unsupported_segment():
    with pytest.raises(DhanProtocolError, match="Unsupported"):
        dhan_subscription(instrument("CUSTOM", "CUSTOM"))


def test_connected_session_reconciles_subscription_deltas():
    class Socket:
        def __init__(self):
            self.sent = []
            self.incoming = asyncio.Queue()

        async def send(self, message):
            self.sent.append(json.loads(message))

        async def recv(self):
            return await self.incoming.get()

    async def scenario():
        async def ignore_packet(*_args):
            pass

        async def ignore_fatal(*_args):
            pass

        row = instrument()
        first = DhanSubscription("one", "NSE_EQ", 1, "1", row)
        second = DhanSubscription("two", "NSE_EQ", 1, "2", row)
        socket = Socket()
        session = DhanFeedSession("user", "client", "secret", 17, 1, ignore_packet, ignore_fatal)
        session.replace_subscriptions([first])
        task = asyncio.create_task(session._connected(socket))
        await asyncio.sleep(0)
        session.replace_subscriptions([second])
        for _ in range(20):
            if len(socket.sent) == 3:
                break
            await asyncio.sleep(0)
        session.stop()
        await task
        return socket.sent

    sent = asyncio.run(scenario())
    assert [message["RequestCode"] for message in sent] == [17, 18, 17]
    assert [message["InstrumentList"][0]["SecurityId"] for message in sent] == ["1", "1", "2"]


def test_connected_session_dispatches_matching_binary_packet():
    class Socket:
        def __init__(self, packet):
            self.packet = packet

        async def send(self, _message):
            pass

        async def recv(self):
            packet, self.packet = self.packet, None
            if packet is not None:
                return packet
            await asyncio.Event().wait()

    async def scenario():
        received = []
        row = instrument()
        subscription = DhanSubscription(row.id, "NSE_EQ", 1, "1333", row)

        async def collect(*args):
            received.append(args)
            session.stop()

        async def ignore_fatal(*_args):
            pass

        packet = (
            OPEN_INTEREST.pack(5, OPEN_INTEREST.size, 1, 1333, 420)
            + TICKER.pack(2, TICKER.size, 1, 1333, 812.25, 1_700_000_000)
        )
        socket = Socket(packet)
        session = DhanFeedSession("user", "client", "secret", 17, 1, collect, ignore_fatal)
        session.replace_subscriptions([subscription])
        await session._connected(socket)
        return received

    received = asyncio.run(scenario())
    assert len(received) == 1
    assert received[0][0] == "user"
    assert received[0][2]["LTP"] == pytest.approx(812.25)
    assert received[0][2]["OI"] == 420
