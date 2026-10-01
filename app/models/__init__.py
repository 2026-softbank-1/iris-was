# 모든 모델을 여기서 import 해야 Alembic autogenerate 가 인식한다.
from app.models.base import Base
from app.models.build import Build
from app.models.deployment_request import DeploymentRequest
from app.models.job import Job
from app.models.project import Project
from app.models.release import Release
from app.models.service import Service
from app.models.target import ServiceTarget, Target
from app.models.user import GithubInstallation, User, UserGithubInstallation

__all__ = [
    "Base",
    "Build",
    "DeploymentRequest",
    "GithubInstallation",
    "Job",
    "Project",
    "Release",
    "Service",
    "ServiceTarget",
    "Target",
    "User",
    "UserGithubInstallation",
]
