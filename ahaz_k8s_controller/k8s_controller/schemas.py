from pydantic import BaseModel


class PodDefinition(BaseModel):
    # May change in the future
    name: str
    visible: bool
    image: str  # Differs from task.yml, since this is resolved when put into Ahaz
    limits: dict[str, str]  # Differs from task.yml
    env: dict[str, str]  # Differs from task.yml
    network: list[str]


class NetworkDefinition(BaseModel):
    name: str
    access: list[str]  # Currently only "internet" and "player" are allowed


class Task(BaseModel):
    name: str
    pods: list[PodDefinition]
    networks: list[NetworkDefinition]


class DBUser(BaseModel):
    id: str
    vpn_config: str | None


class DBTeam(BaseModel):
    name: str
    namespace: str
    vpn_port: int
    vpn_config: str | None
    users: list[DBUser]
