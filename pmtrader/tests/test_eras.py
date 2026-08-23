"""polymarket.eras — the policy-era registry and the rules that keep it honest.

Two halves. The first pins the registry's own invariants (ordered, exhaustive,
half-open) against hand-built registries, so a future boundary that overlaps or
leaves a hole fails here rather than on the operator's scoreboard. The second
checks the SHIPPED registry against the repo's own record — the era tags on the
characterization fixtures — so the committed boundaries cannot drift away from
the evidence cited beside them.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from polymarket import eras
from polymarket.eras import Era

_FIXTURES = Path(__file__).resolve().parents[2] / "pmengine" / "fixtures"


def _reg(*pairs) -> tuple[Era, ...]:
    return tuple(Era(n, s, "why") for n, s in pairs)


# ---------- registry validation ----------

def test_shipped_registry_validates():
    eras.validate()  # also runs at import; this is the explicit assertion


def test_shipped_registry_starts_are_strictly_increasing():
    starts = [e.start for e in eras.ERAS]
    assert starts == sorted(starts)
    assert len(set(starts)) == len(starts)


def test_shipped_registry_opens_at_zero_so_nothing_falls_outside():
    # The exhaustiveness guard: no window ever traded can predate era 0.
    assert eras.ERAS[0].start == 0.0


def test_shipped_registry_names_are_unique_and_bare_tokens():
    names = eras.names()
    assert len(set(names)) == len(names)
    assert all(n and n == n.strip() and " " not in n for n in names)


def test_shipped_registry_states_a_reason_for_every_boundary():
    assert all(e.why.strip() for e in eras.ERAS)


def test_validate_rejects_an_empty_registry():
    with pytest.raises(ValueError, match="empty"):
        eras.validate(())


def test_validate_rejects_a_registry_that_does_not_open_at_zero():
    with pytest.raises(ValueError, match="belong to no era"):
        eras.validate(_reg(("a", 100.0), ("b", 200.0)))


def test_validate_rejects_overlapping_eras():
    with pytest.raises(ValueError, match="strictly increasing"):
        eras.validate(_reg(("a", 0.0), ("b", 300.0), ("c", 200.0)))


def test_validate_rejects_a_zero_length_era():
    # Equal starts leave one era with no windows it can ever own.
    with pytest.raises(ValueError, match="strictly increasing"):
        eras.validate(_reg(("a", 0.0), ("b", 200.0), ("c", 200.0)))


def test_validate_rejects_duplicate_names():
    with pytest.raises(ValueError, match="duplicate"):
        eras.validate(_reg(("a", 0.0), ("a", 200.0)))


def test_validate_rejects_a_name_with_whitespace():
    with pytest.raises(ValueError, match="bare token"):
        eras.validate(_reg(("a", 0.0), ("two words", 200.0)))


def test_validate_rejects_an_unexplained_boundary():
    with pytest.raises(ValueError, match="no stated reason"):
        eras.validate((Era("a", 0.0, "why"), Era("b", 200.0, "  ")))


# ---------- bounds: the half-open partition ----------

def test_bounds_are_half_open_and_adjacent():
    reg = _reg(("a", 0.0), ("b", 100.0), ("c", 200.0))
    assert eras.bounds("a", reg) == (0.0, 100.0)
    assert eras.bounds("b", reg) == (100.0, 200.0)
    # Each era's end IS the next era's start: no gap for a window to vanish in.
    assert [eras.bounds(e, reg)[1] for e in reg[:-1]] == [e.start for e in reg[1:]]


def test_bounds_of_the_running_era_are_open_ended():
    assert eras.bounds(eras.ERAS[-1])[1] == math.inf


def test_bounds_accepts_an_era_or_its_name():
    assert eras.bounds(eras.ERAS[1]) == eras.bounds(eras.ERAS[1].name)


def test_bounds_refuses_an_unknown_era():
    with pytest.raises(KeyError):
        eras.bounds("no-such-era")


# ---------- for_start: which era owns a window ----------

def test_for_start_files_a_window_by_its_start_epoch():
    reg = _reg(("a", 0.0), ("b", 100.0), ("c", 200.0))
    assert eras.for_start(50.0, reg).name == "a"
    assert eras.for_start(150.0, reg).name == "b"
    assert eras.for_start(10_000.0, reg).name == "c"


def test_for_start_puts_a_boundary_window_in_the_NEW_era():
    # Half-open [start, next): a window starting exactly on a deploy ran the
    # new policy, and belongs to exactly one era either way.
    reg = _reg(("a", 0.0), ("b", 100.0))
    assert eras.for_start(99.999, reg).name == "a"
    assert eras.for_start(100.0, reg).name == "b"


def test_for_start_never_returns_none_even_below_the_registry():
    reg = _reg(("a", 0.0), ("b", 100.0))
    assert eras.for_start(-1.0, reg).name == "a"
    assert eras.for_start(0.0, reg).name == "a"


def test_every_era_owns_its_own_start_and_nothing_of_its_neighbour():
    for e in eras.ERAS:
        lo, hi = eras.bounds(e)
        assert eras.for_start(lo).name == e.name
        if hi != math.inf:
            assert eras.for_start(hi).name != e.name


# ---------- current-era detection ----------

def test_current_is_the_newest_era_whose_deploy_has_happened():
    reg = _reg(("a", 0.0), ("b", 100.0), ("c", 200.0))
    assert eras.current(250.0, reg).name == "c"
    assert eras.current(199.0, reg).name == "b"


def test_current_with_a_live_clock_is_the_last_shipped_era():
    # Every boundary is in the past, so the running era is the registry's tail.
    assert eras.current().name == eras.ERAS[-1].name


def test_current_agrees_with_for_start_on_the_same_instant():
    reg = _reg(("a", 0.0), ("b", 100.0), ("c", 200.0))
    for t in (0.0, 99.0, 100.0, 201.0):
        assert eras.current(t, reg) == eras.for_start(t, reg)


# ---------- grounded in the repo's own record ----------

def _fixture_eras() -> list[tuple[str, float, list[str]]]:
    if not _FIXTURES.is_dir():
        pytest.skip("pmengine/fixtures not in this checkout")
    out = []
    for p in sorted(_FIXTURES.glob("*.json")):
        d = json.loads(p.read_text())
        out.append((p.stem, float(d["params"]["start"]), list(d.get("era") or [])))
    assert out, "no fixtures found"
    return out


def test_names_reuse_the_fixture_era_vocabulary():
    # The overlap the fixtures already speak: a window tagged `pre-brake` and a
    # boundary named `theta` must mean the same thing in both places.
    assert "pre-brake" in eras.names()
    assert "theta" in eras.names()


def test_fixture_prebrake_tag_lands_in_the_prebrake_era():
    tagged = [(s, st) for s, st, tags in _fixture_eras() if "pre-brake" in tags]
    assert tagged, "the -$370 fixture carries the pre-brake tag"
    for slug, start in tagged:
        assert eras.for_start(start).name == "pre-brake", slug


def test_fixture_theta_tags_straddle_the_theta_boundary():
    theta = eras.by_name("theta").start
    seen_pre = seen_post = False
    for slug, start, tags in _fixture_eras():
        if "pre-theta" in tags:
            seen_pre = True
            assert start < theta, f"{slug} is tagged pre-theta but starts after the boundary"
        if "post-theta" in tags:
            seen_post = True
            assert start >= theta, f"{slug} is tagged post-theta but starts before the boundary"
    assert seen_pre and seen_post


def test_fixture_post_rtds_tag_lands_in_the_stream_era():
    tagged = [(s, st) for s, st, tags in _fixture_eras() if "post-rtds" in tags]
    assert tagged, "the xrp stream-fed fixture carries the post-rtds tag"
    for slug, start in tagged:
        assert eras.for_start(start).name == "stream", slug


def test_every_fixture_lands_in_exactly_one_era():
    for slug, start, _ in _fixture_eras():
        owners = [e for e in eras.ERAS
                  if eras.bounds(e)[0] <= start < eras.bounds(e)[1]]
        assert len(owners) == 1, f"{slug} landed in {[e.name for e in owners]}"
