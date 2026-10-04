"""FastAPI application: HTTP API, idempotency handling, plan freezing."""
from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import get_db, init_db
from .models import PlanRecord
from .pipette import Pipette
from .rational import format_concentration
from .schemas import PlanRequest
from .solver import SolveInput, TargetIn, solve
from .verify import verify_plan


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()  # create tables on startup (waits for the database)
    yield


app = FastAPI(
    title="Microplate Liquid-Handling Planner",
    version="1.0.0",
    summary="Exact transfer planning for microplate liquid handling "
            "(integer nL volumes, rational concentrations).",
    lifespan=lifespan,
)


@app.exception_handler(HTTPException)
async def _http_handler(request, exc: HTTPException):
    if isinstance(exc.detail, dict):
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(status_code=exc.status_code,
                        content={"error": "error", "message": str(exc.detail)})


@app.exception_handler(RequestValidationError)
async def _validation_handler(request, exc: RequestValidationError):
    details = [
        {"loc": [str(p) for p in e.get("loc", ())], "msg": e.get("msg", ""),
         "type": e.get("type", "")}
        for e in exc.errors()
    ]
    return JSONResponse(status_code=400,
                        content={"error": "invalid_request", "details": details})


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _semantic_errors(req: PlanRequest) -> list[str]:
    errs: list[str] = []
    if req.pipette.min_volume_nl > req.pipette.max_volume_nl:
        errs.append("pipette.min_volume_nl must be <= pipette.max_volume_nl")
    ids = [c.id for c in req.intermediates]
    if len(ids) != len(set(ids)):
        errs.append("duplicate intermediate container id")
    for reserved in ("stock", "diluent"):
        if reserved in ids:
            errs.append(f"intermediate id '{reserved}' is reserved")
    wells = [t.well_id for t in req.targets]
    if len(wells) != len(set(wells)):
        errs.append("duplicate target well_id")
    known = set(ids)
    for t in req.targets:
        if t.via is not None and t.via not in known:
            errs.append(f"target '{t.well_id}' references unknown intermediate "
                        f"container '{t.via}'")
        if t.capacity_nl is None and req.well_capacity_nl is None:
            errs.append(f"target '{t.well_id}' has no capacity: set "
                        "capacity_nl on the target or well_capacity_nl globally")
    return errs


def _to_solve_input(req: PlanRequest) -> SolveInput:
    return SolveInput(
        stock_conc=req.stock.concentration,
        stock_volume_nl=req.stock.volume_nl,
        diluent_volume_nl=req.diluent.volume_nl,
        pipette=Pipette(req.pipette.min_volume_nl, req.pipette.max_volume_nl,
                        req.pipette.step_nl),
        intermediates={c.id: c.capacity_nl for c in req.intermediates},
        targets=[
            TargetIn(
                well_id=t.well_id,
                volume_nl=t.volume_nl,
                conc=t.concentration,
                capacity_nl=t.capacity_nl if t.capacity_nl is not None
                else req.well_capacity_nl,
                via=t.via,
            )
            for t in req.targets
        ],
    )


def _canonical_request(req: PlanRequest) -> str:
    """Normalised canonical form used for the idempotency content hash."""
    payload = {
        "stock": {
            "concentration": format_concentration(req.stock.concentration),
            "volume_nl": req.stock.volume_nl,
        },
        "diluent": {"volume_nl": req.diluent.volume_nl},
        "pipette": {
            "min_volume_nl": req.pipette.min_volume_nl,
            "max_volume_nl": req.pipette.max_volume_nl,
            "step_nl": req.pipette.step_nl,
        },
        "well_capacity_nl": req.well_capacity_nl,
        "intermediates": sorted(
            ({"id": c.id, "capacity_nl": c.capacity_nl}
             for c in req.intermediates),
            key=lambda d: d["id"],
        ),
        "targets": sorted(
            ({
                "well_id": t.well_id,
                "volume_nl": t.volume_nl,
                "concentration": format_concentration(t.concentration),
                "capacity_nl": t.capacity_nl,
                "via": t.via,
            } for t in req.targets),
            key=lambda d: d["well_id"],
        ),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _build_response(req: PlanRequest) -> dict:
    inp = _to_solve_input(req)
    out = solve(inp)
    if not out.ok:
        return {
            "request_id": req.request_id,
            "status": "infeasible",
            "blocked_targets": [
                {"well_id": well_id, "reasons": reasons}
                for well_id, reasons in out.blocked.items()
            ],
            "constraints": out.constraints,
        }
    errors, finals = verify_plan(inp, out.transfers)
    if errors:
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_verification_failed", "messages": errors})
    return {
        "request_id": req.request_id,
        "status": "solved",
        "transfers": [
            {"step": i + 1, "source": t.source, "destination": t.destination,
             "volume_nl": t.volume_nl}
            for i, t in enumerate(out.transfers)
        ],
        "intermediates": [
            {"id": r.cid,
             "concentration": format_concentration(r.conc),
             "prepared_volume_nl": r.prepared_nl,
             "used_volume_nl": r.used_nl,
             "leftover_volume_nl": r.prepared_nl - r.used_nl}
            for r in out.intermediates
        ],
        "wells": [
            {"well_id": t.well_id,
             "final_volume_nl": finals[t.well_id][0],
             "final_concentration": format_concentration(finals[t.well_id][1])}
            for t in inp.targets
        ],
        "usage": {
            "stock_used_nl": out.stock_used_nl,
            "stock_remaining_nl": inp.stock_volume_nl - out.stock_used_nl,
            "diluent_used_nl": out.diluent_used_nl,
            "diluent_remaining_nl": inp.diluent_volume_nl - out.diluent_used_nl,
        },
    }


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@app.post("/v1/plans")
def create_plan(req: PlanRequest, db: Session = Depends(get_db)):
    errors = _semantic_errors(req)
    if errors:
        raise HTTPException(status_code=400,
                            detail={"error": "invalid_request",
                                    "messages": errors})

    canonical = _canonical_request(req)
    content_hash = hashlib.sha256(canonical.encode()).hexdigest()

    rec = (db.query(PlanRecord)
           .filter(PlanRecord.request_id == req.request_id)
           .one_or_none())
    if rec is not None:
        if rec.request_hash == content_hash:
            return JSONResponse(rec.response_json)  # frozen replay
        raise HTTPException(
            status_code=409,
            detail={"error": "request_id_conflict",
                    "message": "request_id was already used with different "
                               "content"})

    response = _build_response(req)

    rec = PlanRecord(request_id=req.request_id,
                     request_hash=content_hash,
                     request_json=json.loads(canonical),
                     status=response["status"],
                     response_json=response)
    db.add(rec)
    try:
        db.commit()
    except IntegrityError:
        # Concurrent insert of the same request_id: replay the winner.
        db.rollback()
        rec = (db.query(PlanRecord)
               .filter(PlanRecord.request_id == req.request_id)
               .one())
        if rec.request_hash == content_hash:
            return JSONResponse(rec.response_json)
        raise HTTPException(
            status_code=409,
            detail={"error": "request_id_conflict",
                    "message": "request_id was already used with different "
                               "content"})
    return JSONResponse(response)


@app.get("/v1/plans/{request_id}")
def get_plan(request_id: str, db: Session = Depends(get_db)):
    rec = (db.query(PlanRecord)
           .filter(PlanRecord.request_id == request_id)
           .one_or_none())
    if rec is None:
        raise HTTPException(status_code=404,
                            detail={"error": "not_found",
                                    "message": f"no plan stored for "
                                               f"request_id '{request_id}'"})
    return JSONResponse(rec.response_json)


@app.get("/healthz")
def healthz(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok"}
