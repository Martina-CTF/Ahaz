# TODO: REMOVE ME!!!!! I SUCK!!!!!
from typing import TypedDict

from pydantic import BaseModel, field_validator


class RegisterProgressDoc(TypedDict):
    team_id: str
    user_id: str
    progress: int


class RegisterProgress(BaseModel):
    team_id: str
    user_id: str
    progress: int

    def __str__(self) -> str:
        return f"RegisterProgress(team_id={self.team_id}, user_id={self.user_id}, progress={self.progress})"

    @field_validator("progress")
    @classmethod
    def validate_progress(cls, v: int) -> int:
        if not (1 <= v <= 9):
            raise ValueError("progress must be between 1 and 9")
        return v
