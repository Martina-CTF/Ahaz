from typing import TypedDict

from pydantic import BaseModel


class TeamDoc(TypedDict):
    team_id: str
    port: int


class Team(BaseModel):
    team_id: str
    port: int
