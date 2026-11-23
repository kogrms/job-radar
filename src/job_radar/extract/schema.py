"""`VacancyFields`, the extraction schema (week-1 spec, PR 4). Sent to the provider as a strict JSON schema:
every field is required, "absent" is an explicit null, no extra keys."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Seniority = Literal["intern", "junior", "middle", "senior", "staff", "principal", "lead", "unknown"]
WorkplaceType = Literal["remote", "hybrid", "onsite", "unknown"]
SalaryPeriod = Literal["year", "month", "week", "day", "hour"]


class Salary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min: float | None
    max: float | None
    currency: str | None = Field(description="ISO 4217 code, e.g. EUR, USD, RSD")
    period: SalaryPeriod | None


class VacancyFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    seniority: Seniority
    location_quote: str | None = Field(
        description="Exact span copied from the posting that states the location / remote policy, or null"
    )
    workplace_type: WorkplaceType
    countries: list[str] = Field(description="Allowed or required work countries, English names")
    regions: list[str] = Field(description="Allowed regions or time zones, as written")
    salary: Salary | None
    stack: list[str] = Field(description="Requested technologies, as written, at most 20")
    language: str = Field(description="ISO 639-1 code of the posting's main language")
