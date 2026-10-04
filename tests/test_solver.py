"""Solver unit tests (no database required)."""
from fractions import Fraction

from app.pipette import Pipette
from app.solver import (SolveInput, TargetIn, solve)
from app.verify import verify_plan

F = Fraction


def make_input(**kw):
    defaults = dict(
        stock_conc=F(1),
        stock_volume_nl=1_000_000,
        diluent_volume_nl=10_000_000,
        pipette=Pipette(min_nl=1000, max_nl=10_000, step_nl=100),
        intermediates={},
        targets=[],
    )
    defaults.update(kw)
    return SolveInput(**defaults)


def check_ok(inp, out):
    assert out.ok, f"expected solvable, got blocked={out.blocked} " \
                   f"constraints={out.constraints}"
    errors, finals = verify_plan(inp, out.transfers)
    assert errors == [], f"plan failed verification: {errors}"
    return finals


def test_direct_two_source_mix():
    inp = make_input(targets=[
        TargetIn("A1", 20_000, F(1, 2), 50_000),
        TargetIn("A2", 16_000, F(1, 4), 50_000),
    ])
    out = solve(inp)
    finals = check_ok(inp, out)
    assert finals["A1"] == (20_000, F(1, 2))
    assert finals["A2"] == (16_000, F(1, 4))
    # A1: 10000 stock + 10000 diluent; A2: 4000 stock + 12000 diluent
    assert out.stock_used_nl == 14_000
    assert out.diluent_used_nl == 22_000


def test_pure_stock_and_pure_diluent():
    inp = make_input(targets=[
        TargetIn("A1", 5_000, F(1), 50_000),
        TargetIn("A2", 6_000, F(0), 50_000),
    ])
    out = solve(inp)
    finals = check_ok(inp, out)
    assert finals["A1"] == (5_000, F(1))
    assert finals["A2"] == (6_000, F(0))


def test_large_volume_is_split_into_valid_chunks():
    inp = make_input(targets=[TargetIn("A1", 35_000, F(1), 100_000)])
    out = solve(inp)
    check_ok(inp, out)
    # 35000 nL with max 10000 -> several transfers, each within limits
    assert len(out.transfers) >= 4
    for tr in out.transfers:
        assert 1000 <= tr.volume_nl <= 10_000
        assert tr.volume_nl % 100 == 0


def test_tiny_solute_volume_uses_intermediate():
    # 5 nL stock-equivalent is below the 1000 nL pipette minimum.
    inp = make_input(
        intermediates={"C1": 500_000},
        targets=[TargetIn("B1", 50_000, F(1, 10_000), 100_000)],
    )
    out = solve(inp)
    finals = check_ok(inp, out)
    assert finals["B1"] == (50_000, F(1, 10_000))
    assert any(r.cid == "C1" for r in out.intermediates)


def test_serial_dilution_two_stages():
    # x = 2.5 / 2 nL stock-equivalent; the shared factor 1000 cannot be
    # prepared in one step inside a 200 uL container -> serial dilution.
    inp = make_input(
        pipette=Pipette(min_nl=1000, max_nl=50_000, step_nl=500),
        intermediates={"C1": 200_000, "C2": 200_000},
        targets=[
            TargetIn("B1", 50_000, F(1, 20_000), 100_000),
            TargetIn("B2", 40_000, F(1, 20_000), 100_000),
        ],
    )
    out = solve(inp)
    finals = check_ok(inp, out)
    assert finals["B1"] == (50_000, F(1, 20_000))
    assert finals["B2"] == (40_000, F(1, 20_000))
    used = {r.cid for r in out.intermediates}
    assert used == {"C1", "C2"}


def test_gap_volume_routed_through_intermediate():
    # min 100 / max 150 / step 10: a 160 nL stock leg is impossible directly
    # (gap between 1x and 2x ranges), but works via a 5/4 dilution.
    inp = make_input(
        pipette=Pipette(min_nl=100, max_nl=150, step_nl=10),
        intermediates={"C1": 100_000},
        targets=[TargetIn("A1", 1600, F(1, 10), 10_000)],
    )
    out = solve(inp)
    finals = check_ok(inp, out)
    assert finals["A1"] == (1600, F(1, 10))


def test_infeasible_concentration_above_stock():
    inp = make_input(targets=[TargetIn("A1", 10_000, F(2), 50_000)])
    out = solve(inp)
    assert not out.ok
    assert out.transfers == []  # no partial plan
    assert "A1" in out.blocked
    assert any("exceeds stock" in r for r in out.blocked["A1"])


def test_infeasible_volume_below_pipette_minimum():
    inp = make_input(targets=[TargetIn("A1", 500, F(1), 50_000)])
    out = solve(inp)
    assert not out.ok
    assert "A1" in out.blocked


def test_infeasible_insufficient_stock():
    inp = make_input(
        stock_volume_nl=5_000,
        targets=[TargetIn("A1", 20_000, F(1, 2), 50_000)],
    )
    out = solve(inp)
    assert not out.ok
    assert any("stock" in c for c in out.constraints)
    assert out.transfers == []


def test_infeasible_without_containers_when_dilution_needed():
    inp = make_input(targets=[TargetIn("B1", 50_000, F(1, 10_000), 100_000)])
    out = solve(inp)
    assert not out.ok
    assert "B1" in out.blocked


def test_infeasible_well_capacity_exceeded():
    inp = make_input(targets=[TargetIn("A1", 60_000, F(1, 2), 50_000)])
    out = solve(inp)
    assert not out.ok
    assert any("capacity" in r for r in out.blocked["A1"])


def test_grouping_shares_one_container():
    inp = make_input(
        intermediates={"C1": 500_000},
        targets=[
            TargetIn("B1", 50_000, F(1, 10_000), 100_000),
            TargetIn("B2", 40_000, F(1, 10_000), 100_000),
            TargetIn("B3", 30_000, F(1, 10_000), 100_000),
        ],
    )
    out = solve(inp)
    check_ok(inp, out)
    # all three wells share the same dilution -> a single container suffices
    assert {r.cid for r in out.intermediates} == {"C1"}


def test_via_pin_routes_through_named_container():
    inp = make_input(
        intermediates={"C1": 500_000, "C2": 500_000},
        targets=[TargetIn("B1", 50_000, F(1, 10_000), 100_000, via="C2")],
    )
    out = solve(inp)
    check_ok(inp, out)
    assert any(r.cid == "C2" for r in out.intermediates)
    assert any(tr.destination == "intermediate:C2" or
               tr.source == "intermediate:C2" for tr in out.transfers)


def test_stock_and_diluent_usage_tracked():
    inp = make_input(
        stock_volume_nl=100_000,
        diluent_volume_nl=1_000_000,
        targets=[TargetIn("A1", 20_000, F(1, 2), 50_000)],
    )
    out = solve(inp)
    check_ok(inp, out)
    assert out.stock_used_nl == 10_000
    assert out.diluent_used_nl == 10_000


def test_exact_rational_dilution_factor():
    # x = 1369 nL is not a multiple of the 100 nL step; the exact factor
    # 100/37 fills the well from the intermediate with no top-up.
    inp = make_input(
        intermediates={"C1": 500_000},
        targets=[TargetIn("A1", 3_700, F(37, 100), 10_000)],
    )
    out = solve(inp)
    finals = check_ok(inp, out)
    assert finals["A1"] == (3_700, F(37, 100))
    assert out.intermediates[0].conc == F(37, 100)
