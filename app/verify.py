"""Independent plan verifier.

Re-simulates a transfer plan against the original request and checks every
stated constraint:

* each transfer respects the pipette min/max/step limits;
* sources are never overdrawn (stock, diluent, intermediate levels);
* destinations never overflow (well capacity, container capacity);
* target wells are never used as a source;
* transfers between intermediate containers form no cycle;
* final well volumes and concentrations match the targets exactly
  (solute mass conservation with exact rational arithmetic).
"""
from __future__ import annotations

from fractions import Fraction

from .solver import (DILUENT, INTERMEDIATE_PREFIX, STOCK, WELL_PREFIX,
                     SolveInput, Transfer)


def verify_plan(inp: SolveInput, transfers: list[Transfer]):
    """Return ``(errors, finals)``.

    ``finals`` maps well_id -> (final_volume_nl, final_concentration) and is
    only meaningful when ``errors`` is empty.
    """
    errors: list[str] = []
    pip = inp.pipette

    well_vol = {t.well_id: 0 for t in inp.targets}
    well_solute = {t.well_id: Fraction(0) for t in inp.targets}
    well_cap = {t.well_id: t.capacity_nl for t in inp.targets}
    cont_vol = {c: 0 for c in inp.intermediates}
    cont_solute = {c: Fraction(0) for c in inp.intermediates}
    stock_left = inp.stock_volume_nl
    diluent_left = inp.diluent_volume_nl
    edges: set[tuple[str, str]] = set()

    for step, tr in enumerate(transfers, 1):
        v = tr.volume_nl
        if v % pip.step_nl != 0 or v < pip.lo or v > pip.hi:
            errors.append(
                f"step {step}: volume {v} nL violates pipette limits "
                f"(effective min {pip.lo} nL, max {pip.hi} nL, step {pip.step_nl} nL)")
        src, dst = tr.source, tr.destination
        if src.startswith(WELL_PREFIX):
            errors.append(f"step {step}: target well '{src}' used as source")
            continue

        # Debit the source.
        if src == STOCK:
            conc = inp.stock_conc
            stock_left -= v
            if stock_left < 0:
                errors.append(f"step {step}: stock overdrawn by {-stock_left} nL")
        elif src == DILUENT:
            conc = Fraction(0)
            diluent_left -= v
            if diluent_left < 0:
                errors.append(f"step {step}: diluent overdrawn by {-diluent_left} nL")
        elif src.startswith(INTERMEDIATE_PREFIX):
            cid = src[len(INTERMEDIATE_PREFIX):]
            if cid not in cont_vol:
                errors.append(f"step {step}: unknown intermediate '{cid}'")
                continue
            if cont_vol[cid] < v:
                errors.append(
                    f"step {step}: intermediate '{cid}' overdrawn "
                    f"(level {cont_vol[cid]} nL, draw {v} nL)")
                conc = Fraction(0)
            else:
                conc = cont_solute[cid] / cont_vol[cid] if cont_vol[cid] else Fraction(0)
                cont_vol[cid] -= v
                cont_solute[cid] -= conc * v
            if dst.startswith(INTERMEDIATE_PREFIX):
                edges.add((cid, dst[len(INTERMEDIATE_PREFIX):]))
        else:
            errors.append(f"step {step}: unknown source '{src}'")
            continue

        # Credit the destination.
        if dst.startswith(WELL_PREFIX):
            wid = dst[len(WELL_PREFIX):]
            if wid not in well_vol:
                errors.append(f"step {step}: unknown target well '{wid}'")
                continue
            well_vol[wid] += v
            well_solute[wid] += conc * v
            if well_vol[wid] > well_cap[wid]:
                errors.append(
                    f"step {step}: well '{wid}' overflows capacity "
                    f"{well_cap[wid]} nL")
        elif dst.startswith(INTERMEDIATE_PREFIX):
            cid = dst[len(INTERMEDIATE_PREFIX):]
            if cid not in cont_vol:
                errors.append(f"step {step}: unknown intermediate '{cid}'")
                continue
            cont_vol[cid] += v
            cont_solute[cid] += conc * v
            if cont_vol[cid] > inp.intermediates[cid]:
                errors.append(
                    f"step {step}: intermediate '{cid}' overflows capacity "
                    f"{inp.intermediates[cid]} nL")
        else:
            errors.append(f"step {step}: unknown destination '{dst}'")

    # Transfers between intermediates must be acyclic.
    if edges:
        indeg = {c: 0 for c in cont_vol}
        adj = {c: [] for c in cont_vol}
        for a, b in edges:
            adj[a].append(b)
            indeg[b] += 1
        queue = [c for c in cont_vol if indeg[c] == 0]
        seen = 0
        while queue:
            node = queue.pop()
            seen += 1
            for nxt in adj[node]:
                indeg[nxt] -= 1
                if indeg[nxt] == 0:
                    queue.append(nxt)
        if seen != len(cont_vol):
            errors.append("cyclic transfers among intermediate containers")

    finals: dict[str, tuple[int, Fraction]] = {}
    for t in inp.targets:
        vol = well_vol[t.well_id]
        conc = well_solute[t.well_id] / vol if vol else Fraction(0)
        finals[t.well_id] = (vol, conc)
        if vol != t.volume_nl:
            errors.append(
                f"well '{t.well_id}': final volume {vol} nL "
                f"!= target {t.volume_nl} nL")
        elif conc != t.conc:
            errors.append(
                f"well '{t.well_id}': final concentration {conc} "
                f"!= target {t.conc}")
    return errors, finals
