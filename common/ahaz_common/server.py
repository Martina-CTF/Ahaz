from pydantic import BaseModel


class ChallengeRequest(BaseModel):
    team_id: str
    challenge_id: str

    def __str__(self):
        return f"ChallengeRequest(team_id={self.team_id}, challenge_id={self.challenge_id})"


class TeamRequest(BaseModel):
    team_id: str

    def __str__(self):
        return f"TeamRequest(team_id={self.team_id})"


class UserRequest(BaseModel):
    team_id: str
    user_id: str

    def __str__(self):
        return f"UserRequest(user_id={self.user_id}, team_id={self.team_id})"


class RegisterTeamRequest(BaseModel):
    team_id: str
    domain_name: str
    port: int
    protocol: str

    def __str__(self):
        return (
            f"RegisterTeamRequest(team_id={self.team_id}, domain_name={self.domain_name}, "
            f"port={self.port}, protocol={self.protocol})"
        )
