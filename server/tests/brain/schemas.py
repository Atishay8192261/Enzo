"""Tool parameter models shared by the brain tests."""

from pydantic import BaseModel, ConfigDict, Field


class SearchWeb(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, description="search terms")
    max_results: int = Field(default=5, ge=1, le=20)
