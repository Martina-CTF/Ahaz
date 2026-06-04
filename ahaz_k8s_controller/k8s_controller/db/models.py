# TODO: Temporary file, reimplementing current DB tables as models
import datetime
from typing import TypedDict

from pydantic import BaseModel


class Team(TypedDict):
    name: str
    # teamID: int # I cbb to reimplement this when I will be changing this schema anyways


class VPNMap(TypedDict):
    teamID: str
    port: int


class VPNStorage(TypedDict):
    teamID: str
    username: str
    config: str


class Challenge(TypedDict):
    name: str
    ctfd_desc: str
    ctfd_score: int
    ctfd_type: str


class Pod(TypedDict):
    name: str
    k8s_name: str
    image: str
    ram: str
    cpu: int
    visible: bool


class NetRule(TypedDict):
    name: str
    netname: str
    k8s_name: str


class EnvVar(TypedDict):
    name: str
    k8s_name: str
    env_var_name: str
    env_var_value: str


class RegisterStatus(TypedDict):
    name: str
    user: str
    state: int
    timestamp: datetime.datetime
