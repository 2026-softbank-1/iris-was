"""GCP 타깃의 Argo CD Application·client·봉인 인증서 선택."""

from typing import Any, cast

import pytest

from app.clients.argocd_client import ArgoCdClient
from app.clients.secret_sealer import SecretSealer
from app.core.exceptions import NotConfiguredError
from app.enums import TargetKind
from app.models.target import Target
from app.services.deploy_service import DeployService, _argo_application_name, _service_path


def _target(name: str, kind: TargetKind) -> Target:
    target = Target(name=name, kind=kind)
    target.onprem_server = None
    return target


def _deployer(**kwargs: Any) -> DeployService:
    return DeployService(
        session_factory=cast(Any, None),
        github=cast(Any, None),
        argocd=cast(ArgoCdClient, "aws-argocd"),
        ecr=cast(Any, None),
        settings=cast(Any, None),
        worker_id="test",
        sealer=cast(SecretSealer, "aws-sealer"),
        **kwargs,
    )


def test_argo_application_name_gcp_target_uses_gcp_prefix() -> None:
    assert _argo_application_name(7, _target("gcp", TargetKind.GCP)) == "gcp-svc-7"
    assert _argo_application_name(7, _target("aws", TargetKind.AWS)) == "svc-7"
    assert _argo_application_name(7, _target("onprem", TargetKind.ONPREM)) == "svc-7"


def test_service_path_gcp_target_uses_gcp_directory() -> None:
    assert _service_path(7, "gcp") == "services/7/gcp"
    assert _service_path(7, "aws") == "services/7/prod"


def test_argocd_for_gcp_target_uses_gcp_client() -> None:
    deployer = _deployer(gcp_argocd=cast(ArgoCdClient, "gcp-argocd"))
    assert deployer._argocd_for(_target("gcp", TargetKind.GCP)) == "gcp-argocd"
    assert deployer._argocd_for(_target("aws", TargetKind.AWS)) == "aws-argocd"


def test_argocd_for_gcp_target_without_token_raises() -> None:
    with pytest.raises(NotConfiguredError):
        _deployer()._argocd_for(_target("gcp", TargetKind.GCP))


def test_target_sealer_gcp_target_uses_gcp_cert() -> None:
    deployer = _deployer(gcp_sealer=cast(SecretSealer, "gcp-sealer"))
    assert deployer._target_sealer(_target("gcp", TargetKind.GCP)) == "gcp-sealer"
    assert deployer._target_sealer(_target("aws", TargetKind.AWS)) == "aws-sealer"
    assert _deployer()._target_sealer(_target("gcp", TargetKind.GCP)) is None
