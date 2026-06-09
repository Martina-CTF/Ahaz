import datetime
from typing import TypedDict

from pydantic import BaseModel


class TaskDeploymentDoc(TypedDict):
    team_id: str
    task_name: str
    expire_time: datetime.datetime


class TaskDeployment(BaseModel):
    team_id: str
    task_name: str
    expire_time: datetime.datetime

    def __str__(self) -> str:
        return (
            f"TaskDeployment(team_id={self.team_id}"
            f", task_name={self.task_name}, expire_time={self.expire_time})"
        )
