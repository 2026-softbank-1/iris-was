"""전용 PostgreSQL에서 배포 방식 저장·요청 스냅샷·Deploy Worker values·기한을 검증한다."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.enums import DeploymentStrategy, DeploymentTrigger, JobKind
from app.models import DeploymentRequest, Service
from app.repositories.build_repository import BuildRepository
from app.repositories.deployment_request_repository import DeploymentRequestRepository
from app.repositories.deployment_status_history_repository import DeploymentStatusHistoryRepository
from app.repositories.job_repository import JobRepository
from app.repositories.service_repository import ServiceRepository
from app.repositories.service_variable_repository import ServiceVariableRepository
from app.services.builder_detection import DeployConfig
from app.services.deploy_service import DEADLINE_MARGIN
from app.services.deployment_request_service import DeploymentRequestService
from app.services.scaling_config import ScalingConfig
from app.services.service_scaling_service import ServiceScalingService
from tests.test_deploy_flow import SETTINGS, Harness, _use_target
from tests.test_scaling_integration import _owner
from tests.worker_support import requires_database, session_factory_with_clean_data

pytestmark = [pytest.mark.integration, requires_database]

TWO_PODS = ScalingConfig.defaults().model_copy(update={"replicas": 2})


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async for factory in session_factory_with_clean_data():
        yield factory


def _requests(session: AsyncSession) -> DeploymentRequestService:
    return DeploymentRequestService(
        DeploymentRequestRepository(session),
        JobRepository(session),
        DeploymentStatusHistoryRepository(session),
        BuildRepository(session),
        ServiceVariableRepository(session),
        ServiceRepository(session),
        deployment_strategy_enabled=True,
    )


def _scaling(session: AsyncSession) -> ServiceScalingService:
    return ServiceScalingService(
        session,
        ServiceRepository(session),
        DeploymentRequestRepository(session),
        BuildRepository(session),
        _requests(session),
    )


async def test_canary_request_snapshots_strategy_and_worker_writes_values_and_deadline(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    h = Harness(
        session_factory, settings=SETTINGS.model_copy(update={"deployment_strategy_enabled": True})
    )
    await h.deploy_successfully()
    assert h.service_id is not None
    async with session_factory.begin() as session:
        service = await session.get_one(Service, h.service_id)
        # 컬럼 기본값으로 만들어진 서비스는 ROLLING 이다.
        assert service.deployment_strategy == DeploymentStrategy.ROLLING
        service.deployment_strategy = DeploymentStrategy.CANARY
        owner_id = await _owner(session, h.service_id)
    async with session_factory() as session:
        detail = await _scaling(session).update_scaling(owner_id, h.service_id, TWO_PODS)
    assert detail.deployment_request_id is not None
    async with session_factory() as session:
        request = await session.get_one(DeploymentRequest, detail.deployment_request_id)
        assert request.requested_deployment_strategy == DeploymentStrategy.CANARY
        assert request.deployment_strategy == DeploymentStrategy.CANARY

    before = datetime.now(UTC)
    await h.run_next(JobKind.DEPLOY)

    tree = h.gitops.commits[h.gitops.head][1][f"services/{h.service_id}/prod"]
    values = json.loads(h.gitops.trees[tree]["values.yaml"])
    assert values["deploymentStrategy"] == "CANARY"
    assert values["replicas"] == 2
    _, release, _ = await h.load(detail.deployment_request_id)
    assert release is not None and release.deadline_at is not None
    # Harness 의 빌드가 기록한 deploy_config 다.
    timeout = DeployConfig.model_validate({"healthcheckTimeout": 60}).healthcheck_timeout
    assert release.deadline_at >= (
        before + timedelta(seconds=timeout) + DEADLINE_MARGIN + timedelta(seconds=60)
    )


async def test_scale_down_below_two_pods_records_rolling_fallback(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    h = Harness(session_factory)
    await h.deploy_successfully()
    assert h.service_id is not None
    async with session_factory.begin() as session:
        service = await session.get_one(Service, h.service_id)
        service.deployment_strategy = DeploymentStrategy.BLUE_GREEN
        service.scaling_config = TWO_PODS.model_dump(mode="json")
        owner_id = await _owner(session, h.service_id)
    async with session_factory() as session:
        detail = await _scaling(session).update_scaling(
            owner_id, h.service_id, ScalingConfig.defaults()
        )
    assert detail.deployment_request_id is not None

    await h.run_next(JobKind.DEPLOY)

    async with session_factory() as session:
        request = await session.get_one(DeploymentRequest, detail.deployment_request_id)
        assert request.requested_deployment_strategy == DeploymentStrategy.BLUE_GREEN
        assert request.deployment_strategy == DeploymentStrategy.ROLLING
    tree = h.gitops.commits[h.gitops.head][1][f"services/{h.service_id}/prod"]
    # 기능을 켜지 않은 Worker 는 이전 chart 가 모르는 키를 쓰지 않는다.
    assert "deploymentStrategy" not in json.loads(h.gitops.trees[tree]["values.yaml"])


@pytest.mark.parametrize(
    ("target_name", "expected"),
    [("aws", DeploymentStrategy.CANARY), ("onprem", DeploymentStrategy.ROLLING)],
)
async def test_request_on_target_applies_strategy_by_target_kind(
    session_factory: async_sessionmaker[AsyncSession],
    target_name: str,
    expected: DeploymentStrategy,
) -> None:
    h = Harness(session_factory)
    await h.deploy_successfully()
    assert h.service_id is not None
    await _use_target(h, target_name)
    async with session_factory.begin() as session:
        service = await session.get_one(Service, h.service_id)
        service.deployment_strategy = DeploymentStrategy.CANARY
        service.scaling_config = TWO_PODS.model_dump(mode="json")

    async with session_factory.begin() as session:
        service = await session.get_one(Service, h.service_id)
        request = await _requests(session).create_deployment_request(
            service,
            source_sha="b" * 40,
            source_commit_message=None,
            trigger_type=DeploymentTrigger.PUSH,
            idempotency_key=f"target-{target_name}",
        )

    assert request is not None
    assert request.requested_deployment_strategy == DeploymentStrategy.CANARY
    assert request.deployment_strategy == expected


async def test_worker_with_flag_on_omits_strategy_key_for_onprem_target(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    h = Harness(
        session_factory, settings=SETTINGS.model_copy(update={"deployment_strategy_enabled": True})
    )
    request_id = await h.request_deploy()
    await _use_target(h, "onprem")
    async with session_factory.begin() as session:
        # 플래그를 켠 Worker 가 받는 요청이라도 on-prem release 에는 키를 쓰지 않는다.
        await session.execute(
            update(DeploymentRequest)
            .where(DeploymentRequest.id == request_id)
            .values(deployment_strategy=DeploymentStrategy.CANARY)
        )

    await h.run_next(JobKind.DEPLOY)

    tree = h.gitops.commits[h.gitops.head][1][f"services/{h.service_id}/onprem"]
    assert "deploymentStrategy" not in json.loads(h.gitops.trees[tree]["values.yaml"])
