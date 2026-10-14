"""Harvest API v2 payload shapes.

UNVERIFIED: help.getharvest.com was unreachable from the build environment.
first_name / last_name / email are believed to be the only required fields on
POST /v2/users; confirm before production use. tests/test_harvest_client.py
pins the shapes this code expects, so a mismatch shows up as a test failure
rather than a runtime surprise.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, EmailStr


class HarvestUserCreate(BaseModel):
    """Request body for POST /v2/users.

    extra="forbid" so a typo'd field name fails here rather than being
    silently dropped by Harvest.
    """

    model_config = ConfigDict(extra="forbid")

    first_name: str
    last_name: str
    email: EmailStr
    is_active: bool = True
    is_contractor: bool = False
    roles: list[str] | None = None
    default_hourly_rate: float | None = None
    weekly_capacity: int | None = None


class HarvestUser(BaseModel):
    """Response shape.

    extra="ignore" -- the mirror image of the request model. Harvest adding a
    field should never break us.
    """

    model_config = ConfigDict(extra="ignore")

    id: int
    first_name: str
    last_name: str
    email: EmailStr
    is_active: bool = True
    is_contractor: bool = False
