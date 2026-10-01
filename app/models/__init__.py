# 모든 모델을 여기서 import 해야 Alembic autogenerate 가 인식한다.
from app.models.base import Base
from app.models.build import Build
from app.models.deployment_request import DeploymentRequest
from app.models.job import Job
from app.models.service import Service
from app.models.user import User

__all__ = ["Base", "Build", "DeploymentRequest", "Job", "Service", "User"]
