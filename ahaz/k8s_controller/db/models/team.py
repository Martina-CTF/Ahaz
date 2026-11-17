from typing import TypedDict

from pydantic import BaseModel


class TeamDoc(TypedDict):
    team_id: str
    port: int
    ta_key: bytes


class Team(BaseModel):
    team_id: str
    port: int
    ta_key: bytes
