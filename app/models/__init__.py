# 모든 모델을 여기서 import 해야 Alembic autogenerate 가 인식한다.
from app.models.base import Base
from app.models.build import Build
from app.models.cli_login_session import CliLoginSession
from app.models.deployment_diagnosis import DeploymentDiagnosis
from app.models.deployment_repair import DeploymentRepair
from app.models.deployment_request import DeploymentRequest
from app.models.deployment_status_history import DeploymentStatusHistory
from app.models.job import Job
from app.models.project import Project
from app.models.release import Release
from app.models.repository_analysis import RepositoryAnalysis
from app.models.service import Service
from app.models.service_stack import ServiceStack, StackDeployment, StackDeploymentStep
from app.models.service_upload import ServiceUpload
from app.models.service_variable import ServiceVariable
from app.models.target import ServiceTarget, Target
from app.models.user import GithubInstallation, User, UserGithubInstallation

__all__ = [
    "Base",
    "Build",
    "CliLoginSession",
    "DeploymentDiagnosis",
    "DeploymentRepair",
    "DeploymentRequest",
    "DeploymentStatusHistory",
    "GithubInstallation",
    "Job",
    "Project",
    "Release",
    "RepositoryAnalysis",
    "Service",
    "ServiceStack",
    "ServiceTarget",
    "ServiceUpload",
    "ServiceVariable",
    "StackDeployment",
    "StackDeploymentStep",
    "Target",
    "User",
    "UserGithubInstallation",
]
