import enum
import re
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

RFC1123_REGEX = re.compile(r"^(?=.{1,63}$)[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?$")


class AccessEnum(enum.Enum):
    player = "player"
    internet = "internet"


class NetworkInformation(BaseModel):
    name: str
    access: list[AccessEnum]


class EnvironmentInformation(BaseModel):
    name: str
    value: str


class LimitInformation(BaseModel):
    ram: str = "128Mi"
    cpu: str = "1"
    ephemeral_storage: str = "128Mi"


class ImageInformation(BaseModel):
    name: str
    context: Optional[str] = None
    registry: Optional[str] = None

    build_args: list[EnvironmentInformation] = Field(default_factory=list)

    def __str__(self) -> str:
        return f"ImageInformation(name={self.name})"


class PodInformation(BaseModel):
    name: str
    visible: bool = False
    image: ImageInformation
    limits: LimitInformation = Field(default_factory=LimitInformation)
    networks: list[str] = Field(default_factory=list)
    env: list[EnvironmentInformation] = Field(default_factory=list)

    exposed_ports: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not RFC1123_REGEX.match(v):
            raise ValueError("name must be a valid RFC 1123 subdomain")
        return v

    @field_validator("exposed_ports")
    @classmethod
    def validate_exposed_ports(cls, v: list[str]) -> list[str]:
        for port in v:
            if not re.match(r"^\d+(/(tcp|udp))?:\d+$", port):
            raise ValueError(f"Invalid port format: {port}")
        return v


    def __str__(self) -> str:
        return f"PodInfo(name={self.name}, visible={self.visible})"


class TaskInformation(BaseModel):
    name: str
    description: Optional[str] = None
    flag: Optional[str] = None

    def __str__(self) -> str:
        return f"TaskInformation(name={self.name})"


class Task(BaseModel):
    name: str
    api_version: Optional[str] = "v1"
    version: Optional[str] = "1.0.0"
    info: Optional[TaskInformation] = None
    pods: list[PodInformation] = Field(default_factory=list)
    networks: list[NetworkInformation] = Field(default_factory=list)

    @model_validator(mode="after")
    def fill_info(self):
        if self.info is None:
            self.info = TaskInformation(name=self.name)
        return self

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not RFC1123_REGEX.match(v):
            raise ValueError("name must be a valid RFC 1123 subdomain")
        return v

    @field_validator("version")
    @classmethod
    def validate_version(cls, v: str) -> str:
        pattern = r"^\d+\.\d+\.\d+$"
        if not re.match(pattern, v):
            raise ValueError("version must be in the format X.Y.Z")
        return v

    def __str__(self) -> str:
        return f"Task(name={self.name}, version={self.version})"
