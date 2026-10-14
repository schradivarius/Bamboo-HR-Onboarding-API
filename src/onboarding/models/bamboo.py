"""BambooHR payload shapes.

Lenient by design. BambooHR returns every field as a string, uses "" rather
than null for empty values, and emits "0000-00-00" for unset dates -- so the
validators below do real work, not decoration.

UNVERIFIED: field names follow BambooHR's documented employee fields, but the
docs host was unreachable from the build environment. Confirm against
/employees/{id}?fields=... output before trusting them in production.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Requested explicitly on every fetch. BambooHR returns only what you ask for.
EMPLOYEE_FIELDS = (
    "id",
    "employeeNumber",
    "firstName",
    "lastName",
    "preferredName",
    "workEmail",
    "hireDate",
    "jobTitle",
    "department",
    "division",
    "status",
    "employmentHistoryStatus",
    "terminationDate",
)


def _blank_to_none(value: Any) -> Any:
    """BambooHR sends "" for empty. Treat it as absent."""
    if isinstance(value, str) and not value.strip():
        return None
    return value


class BambooEmployee(BaseModel):
    """A single employee as returned by GET /employees/{id}."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    employee_number: str | None = Field(default=None, alias="employeeNumber")
    first_name: str | None = Field(default=None, alias="firstName")
    last_name: str | None = Field(default=None, alias="lastName")
    preferred_name: str | None = Field(default=None, alias="preferredName")
    work_email: str | None = Field(default=None, alias="workEmail")
    hire_date: date | None = Field(default=None, alias="hireDate")
    job_title: str | None = Field(default=None, alias="jobTitle")
    department: str | None = Field(default=None, alias="department")
    division: str | None = Field(default=None, alias="division")
    # "Active" / "Inactive"
    status: str | None = Field(default=None, alias="status")
    # Free-text-ish: "Full-Time", "Part-Time", "Contractor", "Terminated", ...
    employment_status: str | None = Field(default=None, alias="employmentHistoryStatus")
    termination_date: date | None = Field(default=None, alias="terminationDate")

    @field_validator("*", mode="before")
    @classmethod
    def _strip_blanks(cls, value: Any) -> Any:
        return _blank_to_none(value)

    @field_validator("hire_date", "termination_date", mode="before")
    @classmethod
    def _tolerate_zero_dates(cls, value: Any) -> Any:
        # BambooHR's unset-date sentinel. Pydantic would raise on it.
        if isinstance(value, str) and value.startswith("0000"):
            return None
        return value

    @field_validator("id", mode="before")
    @classmethod
    def _id_as_str(cls, value: Any) -> Any:
        # Comes back as a string from the REST API but as an int in some
        # webhook payloads.
        return str(value) if value is not None else value

    @property
    def is_active(self) -> bool:
        return (self.status or "").strip().lower() == "active"

    def display_name(self) -> str:
        first = self.preferred_name or self.first_name or ""
        return f"{first} {self.last_name or ''}".strip() or f"employee {self.id}"
