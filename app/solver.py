"""Exact liquid-handling plan solver.

Strategy
--------
1. Targets that can be mixed directly (stock + diluent straight into the well
   with pipettable volumes) are planned directly.
2. Remaining targets are served through intermediate containers: a dilution of
   the stock by an exact rational factor ``d`` is prepared in a container and
   dispensed into the wells.  Targets are grouped so that as few containers as
   possible are used (exact set cover over candidate dilution factors).
3. When a dilution factor is too large to prepare in one step inside the
   container capacity, the planner falls back to serial dilution
   (stock -> intermediate A -> intermediate B -> wells), bounded by
   ``MAX_SERIAL_DEPTH`` and by the number of available containers.

All volumes are integer nanolitres, all concentrations exact rationals, so
every produced plan is exact; :mod:`app.verify` re-checks each plan by
simulation before it is returned.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from functools import lru_cache
from math import gcd

from .pipette import Pipette
from .rational import format_concentration

STOCK = "stock"
DILUENT = "diluent"
WELL_PREFIX = "well:"
INTERMEDIATE_PREFIX = "intermediate:"

MAX_SERIAL_DEPTH = 4
COVER_NODE_LIMIT = 20000
ALIQUOT_SEARCH_LIMIT = 100000


def _lcm(a: int, b: int) -> int:
    return a // gcd(a, b) * b


# --------------------------------------------------------------------------- #
# Input / output model
# --------------------------------------------------------------------------- #

@dataclass
class TargetIn:
    well_id: str
    volume_nl: int
    conc: Fraction
    capacity_nl: int
    via: str | None = None


@dataclass
class SolveInput:
    stock_conc: Fraction
    stock_volume_nl: int
    diluent_volume_nl: int
    pipette: Pipette
    intermediates: dict[str, int]  # container id -> capacity (nL)
    targets: list[TargetIn]


@dataclass
class Transfer:
    source: str        # "stock" | "diluent" | "intermediate:<id>"
    destination: str   # "intermediate:<id>" | "well:<id>"
    volume_nl: int


@dataclass
class IntermediateResult:
    cid: str
    conc: Fraction
    prepared_nl: int
    used_nl: int


@dataclass
class SolveOutput:
    ok: bool
    transfers: list[Transfer] = field(default_factory=list)
    blocked: dict[str, list[str]] = field(default_factory=dict)
    constraints: list[str] = field(default_factory=list)
    intermediates: list[IntermediateResult] = field(default_factory=list)
    stock_used_nl: int = 0
    diluent_used_nl: int = 0


# --------------------------------------------------------------------------- #
# Intermediate preparation geometry
# --------------------------------------------------------------------------- #

@lru_cache(maxsize=None)
def _aliquot(pip: Pipette, ratio: Fraction) -> int | None:
    """Smallest source aliquot ``a`` for a 1:``ratio`` dilution.

    With ``ratio = p/q`` reduced, the companion diluent volume is
    ``b = a*(p-q)/q``.  Writing ``a = q*k`` (required since gcd(p-q, q) = 1),
    ``b`` is a multiple of the step iff ``k`` is a multiple of
    ``step/gcd(step, p-q)``, and ``a`` is a multiple of the step iff ``k`` is
    a multiple of ``step/gcd(step, q)``.  Hence ``a`` must be a multiple of
    ``G = q * lcm(step/gcd(step,q), step/gcd(step,p-q))``; both ``a`` and
    ``b`` must additionally be pipettable totals (no gaps), which the final
    loop guarantees.
    """
    p, q = ratio.numerator, ratio.denominator
    st = pip.step_nl
    if p == q:
        g = _lcm(st, q)  # b == 0: only ``a`` must be pipettable
    else:
        g = q * _lcm(st // gcd(st, q), st // gcd(st, p - q))
    a = -(-pip.lo // g) * g
    if p > q:
        # require b = a*(p-q)/q >= lo
        amin = -(-(pip.lo * q) // (p - q))
        if a < amin:
            a = -(-amin // g) * g
    for _ in range(ALIQUOT_SEARCH_LIMIT):
        b = a * (p - q) // q
        if pip.transferable(a) and (b == 0 or pip.transferable(b)):
            return a
        a += g
    return None


def _serial_seeds(d: Fraction) -> list[Fraction]:
    """Candidate first-stage factors for splitting a large dilution ``d``."""
    f = float(d)
    seeds: set[int] = set()
    root = int(f ** 0.5)
    for k in range(max(2, root - 3), root + 4):
        seeds.add(k)
    p10 = 10
    while p10 < f:
        seeds.add(p10)
        p10 *= 10
    for k in (2, 3, 4, 5, 6, 8, 10, 16, 20, 25, 32, 40, 50, 64,
              100, 125, 200, 250, 500, 1000):
        if k < f:
            seeds.add(k)
    return [Fraction(k) for k in sorted(seeds) if 1 < k < f]


@dataclass
class PrepConfig:
    """Static recipe for preparing one intermediate concentration."""
    factor: Fraction          # dilution factor d: concentration = stock_conc / d
    serial: bool              # True when sourced from another intermediate
    a: int                    # source aliquot per preparation unit (nL)
    b: int                    # diluent per preparation unit (nL)
    unit: int                 # preparation unit volume a + b (nL)
    helper_cid: str | None = None
    helper: "PrepConfig | None" = None


def build_prep_config(
    pip: Pipette,
    d: Fraction,
    cap: int,
    helper_pool: list[tuple[str, int]],
    depth: int = MAX_SERIAL_DEPTH,
) -> PrepConfig | None:
    """Find a feasible preparation recipe for concentration ``stock/d``.

    ``helper_pool`` holds ``(container_id, capacity)`` pairs that may be used
    for serial dilution stages.
    """
    a = _aliquot(pip, d)
    if a is None:
        return None
    unit = a * d.numerator // d.denominator
    if unit <= cap:
        return PrepConfig(
            factor=d,
            serial=False,
            a=a,
            b=a * (d.numerator - d.denominator) // d.denominator,
            unit=unit,
        )
    if depth <= 1 or not helper_pool:
        return None
    for d1 in _serial_seeds(d):
        r = d / d1
        a2 = _aliquot(pip, r)
        if a2 is None:
            continue
        unit2 = a2 * r.numerator // r.denominator
        if unit2 > cap:
            continue
        for hc, hcap in helper_pool:
            rest = [h for h in helper_pool if h[0] != hc]
            sub = build_prep_config(pip, d1, hcap, rest, depth - 1)
            if sub is None:
                continue
            return PrepConfig(
                factor=d,
                serial=True,
                a=a2,
                b=a2 * (r.numerator - r.denominator) // r.denominator,
                unit=unit2,
                helper_cid=hc,
                helper=sub,
            )
    return None


# --------------------------------------------------------------------------- #
# Preparation session: lazily fills one container, interleaved with draws
# --------------------------------------------------------------------------- #

class PrepSession:
    def __init__(self, solver: "Solver", cid: str, cfg: PrepConfig, needed: int):
        self.solver = solver
        self.cid = cid
        self.cfg = cfg
        self.needed = needed
        self.unit = cfg.unit
        self.max_prepared = -(-needed // self.unit) * self.unit if needed else 0
        self.level = 0
        self.prepared = 0
        self.drawn = 0
        self.helper: PrepSession | None = None
        if cfg.helper is not None:
            units = self.max_prepared // self.unit
            helper_needed = units * cfg.a
            self.helper = PrepSession(solver, cfg.helper_cid, cfg.helper, helper_needed)

    @property
    def cap(self) -> int:
        return self.solver.inp.intermediates[self.cid]

    def ensure(self, amount: int) -> bool:
        """Top the container up (emitting transfers) until level >= amount."""
        s = self.solver
        while self.level < amount:
            units_left = (self.max_prepared - self.prepared) // self.unit
            if units_left <= 0:
                return False
            need_units = -(-(amount - self.level) // self.unit)
            room_units = (self.cap - self.level) // self.unit
            k = min(need_units, units_left, room_units)
            if k <= 0:
                return False
            if self.cfg.serial:
                if not self.helper.ensure(k * self.cfg.a):
                    return False
                for _ in range(k):
                    s.emit_total(INTERMEDIATE_PREFIX + self.helper.cid,
                                 INTERMEDIATE_PREFIX + self.cid, self.cfg.a)
                self.helper.level -= k * self.cfg.a
                self.helper.drawn += k * self.cfg.a
            else:
                for _ in range(k):
                    s.emit_total(STOCK, INTERMEDIATE_PREFIX + self.cid,
                                 self.cfg.a)
            if self.cfg.b:
                for _ in range(k):
                    s.emit_total(DILUENT, INTERMEDIATE_PREFIX + self.cid,
                                 self.cfg.b)
            self.level += k * self.unit
            self.prepared += k * self.unit
        return True

    def draw_to_well(self, well_id: str, amount: int) -> bool:
        chunks = self.solver.pip.split(amount, hi=self.cap)
        if chunks is None:
            return False
        for chunk in chunks:
            if not self.ensure(chunk):
                return False
            self.solver.emit(INTERMEDIATE_PREFIX + self.cid,
                             WELL_PREFIX + well_id, chunk)
            self.level -= chunk
            self.drawn += chunk
        return True

    def containers(self) -> list[str]:
        ids = [self.cid]
        if self.helper:
            ids += self.helper.containers()
        return ids

    def sessions(self) -> list["PrepSession"]:
        out = [self]
        if self.helper:
            out += self.helper.sessions()
        return out


# --------------------------------------------------------------------------- #
# Set cover: choose few dilution factors covering all intermediate targets
# --------------------------------------------------------------------------- #

def set_cover(cand: list[dict[Fraction, int]], max_groups: int) -> list[Fraction] | None:
    universe = set(range(len(cand)))
    nodes = [0]

    def dfs(uncovered: set[int], chosen: list[Fraction], budget: int):
        nodes[0] += 1
        if nodes[0] > COVER_NODE_LIMIT:
            return None
        if not uncovered:
            return chosen
        if len(chosen) >= budget:
            return None
        i = min(uncovered, key=lambda j: len(cand[j]))
        for d in cand[i]:
            cover = {j for j in uncovered if d in cand[j]}
            r = dfs(uncovered - cover, chosen + [d], budget)
            if r is not None:
                return r
        return None

    for budget in range(1, max_groups + 1):
        nodes[0] = 0
        r = dfs(set(universe), [], budget)
        if r is not None:
            return r
    return None


def greedy_cover(cand: list[dict[Fraction, int]], max_groups: int) -> list[Fraction] | None:
    uncovered = set(range(len(cand)))
    chosen: list[Fraction] = []
    while uncovered and len(chosen) < max_groups:
        counts: dict[Fraction, int] = {}
        for i in uncovered:
            for d in cand[i]:
                counts[d] = counts.get(d, 0) + 1
        if not counts:
            break
        best = max(counts, key=lambda d: (counts[d], -float(d)))
        chosen.append(best)
        uncovered = {i for i in uncovered if best not in cand[i]}
    return chosen if not uncovered else None


# --------------------------------------------------------------------------- #
# Solver
# --------------------------------------------------------------------------- #

class Solver:
    def __init__(self, inp: SolveInput):
        self.inp = inp
        self.pip = inp.pipette
        self.transfers: list[Transfer] = []
        self.stock_used = 0
        self.diluent_used = 0
        self.used_containers: set[str] = set()
        self.sessions: list[PrepSession] = []

    # -- emission ---------------------------------------------------------- #

    def emit(self, source: str, destination: str, volume: int) -> None:
        """Append one already-valid single transfer."""
        self.transfers.append(Transfer(source, destination, volume))
        if source == STOCK:
            self.stock_used += volume
        elif source == DILUENT:
            self.diluent_used += volume

    def emit_total(self, source: str, destination: str, volume: int) -> None:
        """Emit a (possibly large) total volume, split into valid transfers."""
        chunks = self.pip.split(volume)
        if chunks is None:  # pragma: no cover - guarded by construction
            raise RuntimeError(f"internal: volume {volume} nL is not pipettable")
        for chunk in chunks:
            self.emit(source, destination, chunk)

    # -- candidate dilution factors ---------------------------------------- #

    def candidates(self, t: TargetIn, x: Fraction) -> dict[Fraction, int]:
        """Map dilution factor -> dispense volume for one target.

        ``x`` is the stock-equivalent volume (nL) the well must receive.
        A candidate dispenses ``w = x*d`` of a ``1/d`` dilution plus a diluent
        top-up of ``Vt - w``; both legs must be pipettable.
        """
        pip, vt = self.pip, t.volume_nl
        out: dict[Fraction, int] = {}

        def add(w: int) -> None:
            if w <= 0 or w > vt or not pip.transferable(w):
                return
            y = vt - w
            if y and not pip.transferable(y):
                return
            d = Fraction(w, 1) / x
            if d > 1 or (d == 1 and t.via):
                out.setdefault(d, w)

        lo, hi, st = pip.lo, pip.hi, pip.step_nl
        w, c = lo, 0
        while w <= min(hi, vt) and c < 200:          # single-chunk dispenses
            add(w)
            w += st
            c += 1
        y, c = lo, 0
        while y <= min(hi, vt - lo) and c < 200:     # small diluent top-ups
            add(vt - y)
            y += st
            c += 1
        add(vt)                                       # no top-up at all
        for n in (2, 3, 4):                           # multi-chunk dispenses
            w, c = n * lo, 0
            while w <= min(n * hi, vt) and c < 60:
                add(w)
                w += st
                c += 1
        return out

    def prep_possible(self, d: Fraction) -> bool:
        caps = sorted(self.inp.intermediates.values(), reverse=True)
        if not caps:
            return False
        pool = [(f"#{i}", c) for i, c in enumerate(caps)]
        return build_prep_config(self.pip, d, pool[0][1], pool[1:]) is not None

    def _factor_key(self, d: Fraction):
        """Niceness ordering for candidate factors: compact, simple first."""
        a = _aliquot(self.pip, d)
        unit = (a * d.numerator // d.denominator) if a is not None else None
        return (unit is None, unit or 0, d.denominator, float(d))

    # -- run one dilution group --------------------------------------------- #

    def run_group(self, d: Fraction, members: list[tuple[TargetIn, int, int]],
                  pinned: str | None) -> bool:
        """Prepare ``stock/d`` in one container and dispense to ``members``.

        ``members`` is a list of ``(target, intermediate_volume, topup)``.
        On failure every emitted transfer is rolled back.
        """
        total = sum(w for _, w, _ in members)
        free = [(c, cap) for c, cap in self.inp.intermediates.items()
                if c not in self.used_containers]
        free.sort(key=lambda p: -p[1])
        if pinned is not None:
            choices = [(pinned, self.inp.intermediates[pinned])]
        else:
            choices = free
        snapshot = (len(self.transfers), self.stock_used, self.diluent_used)
        for cid, cap in choices:
            if cid in self.used_containers:
                continue
            helpers = [(c, c2) for c, c2 in free if c != cid]
            cfg = build_prep_config(self.pip, d, cap, helpers)
            if cfg is None:
                continue
            sess = PrepSession(self, cid, cfg, total)
            ok = True
            for t, w, y in members:
                if w and not sess.draw_to_well(t.well_id, w):
                    ok = False
                    break
                if y:
                    self.emit_total(DILUENT, WELL_PREFIX + t.well_id, y)
            if not ok:
                del self.transfers[snapshot[0]:]
                self.stock_used, self.diluent_used = snapshot[1], snapshot[2]
                continue
            self.used_containers.update(sess.containers())
            self.sessions.append(sess)
            return True
        return False


def _coalesce(transfers: list[Transfer], pip: Pipette) -> list[Transfer]:
    """Merge runs of identical source->destination transfers into fewer steps."""
    out: list[Transfer] = []
    i, n = 0, len(transfers)
    while i < n:
        j = i + 1
        total = transfers[i].volume_nl
        while (j < n
               and transfers[j].source == transfers[i].source
               and transfers[j].destination == transfers[i].destination):
            total += transfers[j].volume_nl
            j += 1
        chunks = pip.split(total) if j - i > 1 else None
        if chunks:
            out.extend(Transfer(transfers[i].source, transfers[i].destination, c)
                       for c in chunks)
        else:
            out.extend(transfers[i:j])
        i = j
    return out


def _infeasible(s: Solver, blocked: dict[str, list[str]],
                constraints: list[str]) -> SolveOutput:
    # Never return a partial plan.
    return SolveOutput(ok=False, blocked=blocked, constraints=constraints,
                       stock_used_nl=s.stock_used, diluent_used_nl=s.diluent_used)


def solve(inp: SolveInput) -> SolveOutput:
    s = Solver(inp)
    pip = inp.pipette
    blocked: dict[str, list[str]] = {}
    constraints: list[str] = []

    def block(t: TargetIn, msg: str) -> None:
        blocked.setdefault(t.well_id, []).append(msg)

    if pip.hi < pip.lo:
        for t in inp.targets:
            block(t, "pipette cannot transfer anything: no multiple of "
                     f"{pip.step_nl} nL lies within "
                     f"[{pip.min_nl}, {pip.max_nl}] nL")
        return _infeasible(s, blocked, constraints)

    cs = inp.stock_conc
    direct: list[tuple[TargetIn, int, int]] = []
    inter: list[tuple[TargetIn, Fraction]] = []

    for t in inp.targets:
        if t.volume_nl > t.capacity_nl:
            block(t, f"final volume {t.volume_nl} nL exceeds well capacity "
                     f"{t.capacity_nl} nL")
            continue
        if t.conc == 0:
            if t.via:
                block(t, "target concentration is 0; routing it through an "
                         "intermediate container is meaningless")
                continue
            if pip.transferable(t.volume_nl):
                direct.append((t, 0, t.volume_nl))
            else:
                block(t, f"required diluent volume {t.volume_nl} nL is not "
                         f"pipettable (effective min {pip.lo} nL, max {pip.hi} nL, "
                         f"step {pip.step_nl} nL)")
            continue
        if cs == 0:
            block(t, "stock concentration is 0 but the target requires solute")
            continue
        if t.conc > cs:
            block(t, f"target concentration {format_concentration(t.conc)} exceeds "
                     f"stock concentration {format_concentration(cs)}")
            continue
        x = t.volume_nl * t.conc / cs  # stock-equivalent volume (exact)
        if not t.via and x.denominator == 1:
            xi, yi = x.numerator, t.volume_nl - x.numerator
            if (xi == 0 or pip.transferable(xi)) and (yi == 0 or pip.transferable(yi)):
                direct.append((t, xi, yi))
                continue
        inter.append((t, x))

    # Evaluate intermediate routes for every remaining target, even when
    # other targets are already blocked, so the infeasibility report is
    # as complete as possible.
    cand_list: list[dict[Fraction, int]] = []
    for t, x in inter:
        cand = {d: w for d, w in s.candidates(t, x).items()
                if s.prep_possible(d)}
        if not cand:
            block(t, "no feasible dilution route: the stock-equivalent volume "
                     f"{format_concentration(x)} nL cannot be pipetted directly "
                     "and no intermediate dilution fits the pipette limits, "
                     "container capacities and container count")
        # Try compact, simple dilution factors first.
        cand = dict(sorted(cand.items(), key=lambda kv: s._factor_key(kv[0])))
        cand_list.append(cand)

    if blocked:
        return _infeasible(s, blocked, constraints)

    for t, xi, yi in direct:
        if xi:
            s.emit_total(STOCK, WELL_PREFIX + t.well_id, xi)
        if yi:
            s.emit_total(DILUENT, WELL_PREFIX + t.well_id, yi)

    if inter:
        max_groups = len(inp.intermediates)
        factors = set_cover(cand_list, max_groups)
        if factors is None:
            factors = greedy_cover(cand_list, max_groups)
        if factors is None:
            for t, _ in inter:
                block(t, "no compatible dilution group fits within the available "
                         "intermediate containers")
            constraints.append(
                f"insufficient intermediate containers: {len(inter)} target(s) "
                f"need intermediate dilution but only {max_groups} container(s) "
                "are available")
            return _infeasible(s, blocked, constraints)

        groups: dict[Fraction, list[int]] = {d: [] for d in factors}
        for i in range(len(inter)):
            for d in factors:
                if d in cand_list[i]:
                    groups[d].append(i)
                    break

        unit_groups: list[tuple[Fraction, list[int], str | None]] = []
        for d, idxs in groups.items():
            by_pin: dict[str | None, list[int]] = {}
            for i in idxs:
                by_pin.setdefault(inter[i][0].via, []).append(i)
            for pin, ids in by_pin.items():
                unit_groups.append((d, ids, pin))
        # Pinned groups first so their container is still free.
        unit_groups.sort(key=lambda g: g[2] is None)

        for d, ids, pin in unit_groups:
            members = []
            for i in ids:
                t, x = inter[i]
                w = int(x * d)  # exact: d was derived from this w
                members.append((t, w, t.volume_nl - w))
            if pin is not None and pin in s.used_containers:
                for t, _, _ in members:
                    block(t, f"pinned intermediate '{pin}' is already consumed by "
                             "another dilution group")
                continue
            if not s.run_group(d, members, pin):
                where = (f"pinned container '{pin}'" if pin
                         else "any free intermediate container")
                for t, _, _ in members:
                    block(t, f"cannot stage dilution 1/{format_concentration(d)} "
                             f"in {where} within its capacity")
        if blocked:
            return _infeasible(s, blocked, constraints)

    if s.stock_used > inp.stock_volume_nl:
        constraints.append(
            f"insufficient stock: plan requires {s.stock_used} nL, "
            f"available {inp.stock_volume_nl} nL")
    if s.diluent_used > inp.diluent_volume_nl:
        constraints.append(
            f"insufficient diluent: plan requires {s.diluent_used} nL, "
            f"available {inp.diluent_volume_nl} nL")
    if constraints:
        return _infeasible(s, blocked, constraints)

    transfers = _coalesce(s.transfers, pip)
    intermediates = [
        IntermediateResult(cid=sub.cid,
                           conc=cs / sub.cfg.factor,
                           prepared_nl=sub.prepared,
                           used_nl=sub.drawn)
        for sess in s.sessions
        for sub in sess.sessions()
    ]
    return SolveOutput(ok=True, transfers=transfers, intermediates=intermediates,
                       stock_used_nl=s.stock_used, diluent_used_nl=s.diluent_used)
