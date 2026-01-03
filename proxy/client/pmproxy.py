"""
pmproxy gRPC client for Python

This client connects to the pmproxy gRPC server to interact with Polymarket APIs.
"""

import grpc
import json
from typing import Optional
from dataclasses import dataclass


@dataclass
class OrderBookLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    token_id: str
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
    raw: dict  # Raw JSON response


@dataclass
class Token:
    token_id: str
    outcome: str
    price: float


@dataclass
class Market:
    id: str
    question: str
    condition_id: str
    slug: str
    volume: float
    liquidity: float
    active: bool
    closed: bool
    tokens: list[Token]


@dataclass
class Event:
    id: str
    slug: str
    title: str
    description: str
    end_date: str
    liquidity: float
    volume: float
    closed: bool
    markets: list[Market]


@dataclass
class Tag:
    id: str
    label: str
    slug: str


class ClobClient:
    """CLOB API client over gRPC"""

    def __init__(self, stub):
        self._stub = stub

    def ok(self) -> bool:
        """Health check"""
        from . import polymarket_pb2
        response = self._stub.Ok(polymarket_pb2.Empty())
        return response.ok

    def sampling_markets(self) -> tuple[list[Market], dict]:
        """Get sampling markets with order books"""
        from . import polymarket_pb2
        response = self._stub.SamplingMarkets(polymarket_pb2.SamplingMarketsRequest())
        markets = [_proto_to_market(m) for m in response.markets]
        raw = json.loads(response.raw_json) if response.raw_json else {}
        return markets, raw

    def order_book(self, token_id: str) -> OrderBook:
        """Get full order book for a token"""
        from . import polymarket_pb2
        response = self._stub.OrderBook(polymarket_pb2.OrderBookRequest(token_id=token_id))
        return OrderBook(
            token_id=response.token_id,
            bids=[OrderBookLevel(price=l.price, size=l.size) for l in response.bids],
            asks=[OrderBookLevel(price=l.price, size=l.size) for l in response.asks],
            raw=json.loads(response.raw_json) if response.raw_json else {},
        )

    def midpoint(self, token_id: str) -> float:
        """Get midpoint price"""
        from . import polymarket_pb2
        response = self._stub.Midpoint(polymarket_pb2.MidpointRequest(token_id=token_id))
        return response.price

    def price(self, token_id: str, side: str) -> float:
        """Get best bid or ask price"""
        from . import polymarket_pb2
        side_enum = polymarket_pb2.BUY if side.upper() == "BUY" else polymarket_pb2.SELL
        response = self._stub.Price(polymarket_pb2.PriceRequest(token_id=token_id, side=side_enum))
        return response.price

    def spread(self, token_id: str) -> tuple[float, float, float]:
        """Get bid, ask, and spread"""
        from . import polymarket_pb2
        response = self._stub.Spread(polymarket_pb2.SpreadRequest(token_id=token_id))
        return response.bid, response.ask, response.spread


class GammaClient:
    """Gamma API client over gRPC"""

    def __init__(self, stub):
        self._stub = stub

    def events(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        closed: Optional[bool] = None,
        order: Optional[str] = None,
        ascending: Optional[bool] = None,
        end_date_min: Optional[str] = None,
        end_date_max: Optional[str] = None,
    ) -> tuple[list[Event], dict]:
        """List events with pagination and filtering"""
        from . import polymarket_pb2
        request = polymarket_pb2.EventsRequest()
        if limit is not None:
            request.limit = limit
        if offset is not None:
            request.offset = offset
        if closed is not None:
            request.closed = closed
        if order is not None:
            request.order = order
        if ascending is not None:
            request.ascending = ascending
        if end_date_min is not None:
            request.end_date_min = end_date_min
        if end_date_max is not None:
            request.end_date_max = end_date_max

        response = self._stub.Events(request)
        events = [_proto_to_event(e) for e in response.events]
        raw = json.loads(response.raw_json) if response.raw_json else {}
        return events, raw

    def event_by_slug(self, slug: str) -> tuple[Optional[Event], dict]:
        """Get event by slug"""
        from . import polymarket_pb2
        response = self._stub.EventBySlug(polymarket_pb2.SlugRequest(slug=slug))
        event = _proto_to_event(response.event) if response.event else None
        raw = json.loads(response.raw_json) if response.raw_json else {}
        return event, raw

    def markets(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> tuple[list[Market], dict]:
        """List markets"""
        from . import polymarket_pb2
        request = polymarket_pb2.MarketsRequest()
        if limit is not None:
            request.limit = limit
        if offset is not None:
            request.offset = offset

        response = self._stub.Markets(request)
        markets = [_proto_to_market(m) for m in response.markets]
        raw = json.loads(response.raw_json) if response.raw_json else {}
        return markets, raw

    def market_by_slug(self, slug: str) -> tuple[Optional[Market], dict]:
        """Get market by slug"""
        from . import polymarket_pb2
        response = self._stub.MarketBySlug(polymarket_pb2.SlugRequest(slug=slug))
        market = _proto_to_market(response.market) if response.market else None
        raw = json.loads(response.raw_json) if response.raw_json else {}
        return market, raw

    def tags(self) -> list[Tag]:
        """Get all tags"""
        from . import polymarket_pb2
        response = self._stub.Tags(polymarket_pb2.Empty())
        return [Tag(id=t.id, label=t.label, slug=t.slug) for t in response.tags]

    def events_by_tag(self, tag_id: str) -> tuple[list[Event], dict]:
        """Get events by tag"""
        from . import polymarket_pb2
        response = self._stub.EventsByTag(polymarket_pb2.TagRequest(tag_id=tag_id))
        events = [_proto_to_event(e) for e in response.events]
        raw = json.loads(response.raw_json) if response.raw_json else {}
        return events, raw

    def search(self, query: str) -> tuple[list[Market], dict]:
        """Search markets"""
        from . import polymarket_pb2
        response = self._stub.Search(polymarket_pb2.SearchRequest(query=query))
        markets = [_proto_to_market(m) for m in response.markets]
        raw = json.loads(response.raw_json) if response.raw_json else {}
        return markets, raw

    def series(self) -> dict:
        """Get recurring series"""
        from . import polymarket_pb2
        response = self._stub.Series(polymarket_pb2.Empty())
        return json.loads(response.raw_json) if response.raw_json else {}


class ChainClient:
    """Chain/RPC client over gRPC"""

    def __init__(self, stub):
        self._stub = stub

    def usdc_balance(self, address: str) -> float:
        """Get USDC balance for address"""
        from . import polymarket_pb2
        response = self._stub.UsdcBalance(polymarket_pb2.AddressRequest(address=address))
        return response.balance

    def token_balance(self, address: str, token_id: str) -> float:
        """Get token balance for address"""
        from . import polymarket_pb2
        response = self._stub.TokenBalance(
            polymarket_pb2.TokenBalanceRequest(address=address, token_id=token_id)
        )
        return response.balance


class PmProxy:
    """
    Main client for pmproxy gRPC server.

    Provides access to CLOB, Gamma, and Chain APIs through a single connection.

    Example:
        proxy = PmProxy("localhost:50051")

        # CLOB operations
        markets, _ = proxy.clob.sampling_markets()
        book = proxy.clob.order_book("token_id")

        # Gamma operations
        events, _ = proxy.gamma.events(limit=100)

        # Chain operations
        balance = proxy.chain.usdc_balance("0x...")
    """

    def __init__(self, target: str = "localhost:50051", secure: bool = False):
        """
        Connect to pmproxy server.

        Args:
            target: gRPC server address (host:port)
            secure: Use TLS if True
        """
        if secure:
            self._channel = grpc.secure_channel(target, grpc.ssl_channel_credentials())
        else:
            self._channel = grpc.insecure_channel(target)

        from . import polymarket_pb2_grpc
        self._clob_stub = polymarket_pb2_grpc.ClobStub(self._channel)
        self._gamma_stub = polymarket_pb2_grpc.GammaStub(self._channel)
        self._chain_stub = polymarket_pb2_grpc.ChainStub(self._channel)

        self.clob = ClobClient(self._clob_stub)
        self.gamma = GammaClient(self._gamma_stub)
        self.chain = ChainClient(self._chain_stub)

    def close(self):
        """Close the connection"""
        self._channel.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# Helper functions for converting protobuf to dataclasses

def _proto_to_token(t) -> Token:
    return Token(
        token_id=t.token_id,
        outcome=t.outcome,
        price=t.price,
    )


def _proto_to_market(m) -> Market:
    return Market(
        id=m.id,
        question=m.question,
        condition_id=m.condition_id,
        slug=m.slug,
        volume=m.volume,
        liquidity=m.liquidity,
        active=m.active,
        closed=m.closed,
        tokens=[_proto_to_token(t) for t in m.tokens],
    )


def _proto_to_event(e) -> Event:
    return Event(
        id=e.id,
        slug=e.slug,
        title=e.title,
        description=e.description,
        end_date=e.end_date,
        liquidity=e.liquidity,
        volume=e.volume,
        closed=e.closed,
        markets=[_proto_to_market(m) for m in e.markets],
    )
