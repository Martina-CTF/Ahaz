# TODO: Temporary file, reimplementing current DB tables as models
import datetime
from typing import TypedDict

from ahaz_common.task import Task as Task
from pydantic import BaseModel, field_validator


class Certificate(TypedDict):
    serial_number: int
    common_name: str
    cert: str
    private_key: str


class CertificateModel(BaseModel):
    serial_number: int
    common_name: str
    cert: str
    private_key: str

    @field_validator("cert", "private_key")
    @classmethod
    def validate_pem(cls, v: str) -> str:
        if not v.startswith("-----BEGIN") or not v.endswith("-----"):
            raise ValueError("Invalid PEM format")
        return v


class TaskDefinition(TypedDict):
    name: str
    definition: Task


class Range(TypedDict):
    team_id: str
    port: int


class TaskDeployment(TypedDict):
    team_id: str
    task_name: str
    expire_time: datetime.datetime
