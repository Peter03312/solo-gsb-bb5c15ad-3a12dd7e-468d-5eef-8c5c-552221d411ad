"""Request/response schemas for the planning API."""
from __future__ import annotations

from fractions import Fraction
from typing import Annotated

from pydantic import (BaseModel, BeforeValidator, ConfigDict, Field,
                      PlainSerializer, WithJsonSchema)

from .rational import format_concentration, parse_concentration

Concentration = Annotated[
    Fraction,
    BeforeValidator(parse_concentration),
    PlainSerializer(format_concentration, return_type=str),
    WithJsonSchema({"type": "string", "examples": ["1/100", "0.25", "3"]}),
]


class PipetteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_volume_nl: int = Field(ge=0, description="minimum single transfer (nL)")
    max_volume_nl: int = Field(gt=0, description="maximum single transfer (nL)")
    step_nl: int = Field(gt=0, description="volume increment (nL)")


class StockIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    concentration: Concentration
    volume_nl: int = Field(ge=0, description="available stock volume (nL)")


class DiluentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    volume_nl: int = Field(ge=0, description="available diluent volume (nL)")


class IntermediateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=64)
    capacity_nl: int = Field(gt=0)


class TargetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    well_id: str = Field(min_length=1, max_length=64)
    volume_nl: int = Field(gt=0, description="required final volume (nL)")
    concentration: Concentration
    capacity_nl: int | None = Field(
        default=None, gt=0,
        description="per-well capacity; falls back to well_capacity_nl")
    via: str | None = Field(
        default=None,
        description="optional id of an intermediate container this well must "
                    "be served from")


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=128)
    stock: StockIn
    diluent: DiluentIn
    pipette: PipetteIn
    well_capacity_nl: int | None = Field(
        default=None, gt=0,
        description="default well capacity when a target has no capacity_nl")
    intermediates: list[IntermediateIn] = Field(default_factory=list)
    targets: list[TargetIn] = Field(min_length=1)
