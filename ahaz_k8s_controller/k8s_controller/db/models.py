# TODO: Temporary file, reimplementing current DB tables as models
import datetime

from pydantic import BaseModel


class Team(BaseModel):
    name: str
    # teamID: int # I cbb to reimplement this when I will be changing this schema anyways

    def __str__(self):
        return f"Team(name={self.name})"


class VPNMap(BaseModel):
    teamID: int
    port: int

    def __str__(self):
        return f"VPNMap(teamID={self.teamID}, port={self.port})"


class VPNStorage(BaseModel):
    teamID: int
    username: str
    config: str

    def __str__(self):
        return f"VPNStorage(teamID={self.teamID}, username={self.username}, config={self.config})"


class Challenge(BaseModel):
    name: str
    ctfd_desc: str
    ctfd_score: int
    ctfd_type: str

    def __str__(self):
        return (
            f"Challenge(name={self.name}, ctfd_desc={self.ctfd_desc}, "
            f"ctfd_score={self.ctfd_score}, ctfd_type={self.ctfd_type})"
        )


class Pod(BaseModel):
    name: str
    k8s_name: str
    image: str
    ram: str
    cpu: int
    visible: bool

    def __str__(self):
        return (
            f"Challenge(name={self.name}, k8s_name={self.k8s_name}, image={self.image}, "
            f"ram={self.ram}, cpu={self.cpu}, visible={self.visible})"
        )


class NetRule(BaseModel):
    name: str
    netname: str
    k8s_name: str

    def __str__(self):
        return f"NetRule(name={self.name}, netname={self.netname}, k8s_name={self.k8s_name})"


class EnvVar(BaseModel):
    name: str
    k8s_name: str
    env_var_name: str
    env_var_value: str

    def __str__(self):
        return (
            f"EnvVar(name={self.name}, k8s_name={self.k8s_name}, env_var_name={self.env_var_name}, "
            f"env_var_value={self.env_var_value})"
        )


class RegisterStatus(BaseModel):
    name: str
    user: str
    state: int
    timestamp: datetime.datetime

    def __str__(self):
        return (
            f"RegisterStatus(name={self.name}, user={self.user}, "
            f"state={self.state}, timestamp={self.timestamp})"
        )
