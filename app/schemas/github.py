from app.clients.source_repository_client import BranchInfo
from app.models.user import GithubInstallation
from app.schemas.response import ApiModel
from app.services.source_repository_service import RepositoryCandidate


class InstallationResponse(ApiModel):
    installation_id: int
    account_login: str
    account_type: str

    @classmethod
    def from_model(cls, installation: GithubInstallation) -> "InstallationResponse":
        return cls(
            installation_id=installation.installation_id,
            account_login=installation.account_login,
            account_type=installation.account_type,
        )


class RepositoryResponse(ApiModel):
    full_name: str
    url: str
    default_branch: str
    is_private: bool
    installation_id: int

    @classmethod
    def from_candidate(cls, candidate: RepositoryCandidate) -> "RepositoryResponse":
        return cls(
            full_name=candidate.full_name,
            url=candidate.url,
            default_branch=candidate.default_branch,
            is_private=candidate.is_private,
            installation_id=candidate.installation_id,
        )


class BranchResponse(ApiModel):
    name: str
    is_default: bool

    @classmethod
    def from_info(cls, info: BranchInfo) -> "BranchResponse":
        return cls(name=info.name, is_default=info.is_default)
