"""The immutable-horizon splice: the all-time ledger outlives the offset cap.

data-api refuses offsets past 5000, so a full offset-walk dies for good near
~5,300 rows. The splice re-walks only the mutable 48h window and takes the
immutable past from the dump — partitioned by TIMESTAMP, so the draft/final
identity trap documented in wallet.py's autopsy cannot arise.
"""
import json

from polymarket import wallet


def _row(ts, slug="btc-updown-5m-1", kind="TRADE", usd=1.0):
    return {"timestamp": ts, "slug": slug, "type": kind, "usdcSize": usd,
            "price": 0.9, "size": 1.0, "outcome": "Up"}


def _dump(tmp_path, rows):
    p = tmp_path / "activity.jsonl"
    with p.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return p


def test_splice_partitions_by_timestamp_with_no_gap_and_no_overlap(tmp_path, monkeypatch):
    now = 1_000_000.0
    cut = now - wallet.IMMUTABLE_AFTER_S
    dump = _dump(tmp_path, [_row(cut - 100, usd=1), _row(cut - 1, usd=2),
                            _row(cut + 5, usd=3)])  # the +5 row is a STALE draft
    walked = [_row(cut + 5, usd=30), _row(cut + 50, usd=4)]  # fresh truth
    monkeypatch.setattr(wallet, "fetch_wallet_activity", lambda a, f: list(walked))
    rows = wallet.activity_since("0xA", 0.0, now=now, path=dump)
    tss = [r["timestamp"] for r in rows]
    assert tss == sorted(tss)
    assert [r["usdcSize"] for r in rows] == [1, 2, 30, 4], (
        "the mutable-window row must come from the WALK (fresh), never the dump draft")


def test_shallow_floor_degenerates_to_a_plain_walk(monkeypatch, tmp_path):
    now = 1_000_000.0
    called = {}
    monkeypatch.setattr(wallet, "fetch_wallet_activity",
                        lambda a, f: called.setdefault("floor", f) or [])
    wallet.activity_since("0xA", now - 60, now=now, path=tmp_path / "none.jsonl")
    assert called["floor"] == now - 60


def test_missing_or_stale_dump_falls_back_to_the_full_walk(monkeypatch, tmp_path):
    now = 1_000_000.0
    cut = now - wallet.IMMUTABLE_AFTER_S
    calls = []
    monkeypatch.setattr(wallet, "fetch_wallet_activity",
                        lambda a, f: calls.append(f) or [])
    # missing dump
    wallet.activity_since("0xA", 0.0, now=now, path=tmp_path / "missing.jsonl")
    # stale dump: coverage ends before the cut
    stale = _dump(tmp_path, [_row(cut - 5000)])
    wallet.activity_since("0xA", 0.0, now=now, path=stale)
    assert calls == [0.0, 0.0], "both cases must walk from the true floor, not splice a gap"


def test_deep_floor_bounds_the_dump_side(tmp_path, monkeypatch):
    now = 1_000_000.0
    cut = now - wallet.IMMUTABLE_AFTER_S
    # The cut+1 row is what QUALIFIES the dump: coverage must reach the cut,
    # or the gap between its last row and the cut would silently vanish.
    dump = _dump(tmp_path, [_row(cut - 500), _row(cut - 100), _row(cut - 10),
                            _row(cut + 1)])
    monkeypatch.setattr(wallet, "fetch_wallet_activity", lambda a, f: [])
    rows = wallet.activity_since("0xA", cut - 150, now=now, path=dump)
    assert [r["timestamp"] for r in rows] == [cut - 100, cut - 10], (
        "dump side is [floor, cut) exactly — the cut+1 row belongs to the walk, "
        "which returned nothing here")
