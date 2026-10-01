from app.models.user import User
from app.schemas.response import ApiModel


class UserResponse(ApiModel):
    id: int
    github_id: int
    login: str
    avatar_url: str | None = None

    @classmethod
    def from_model(cls, user: User) -> "UserResponse":
        return cls(
            id=user.id, github_id=user.github_id, login=user.login, avatar_url=user.avatar_url
        )
