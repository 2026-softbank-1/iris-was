"""등록한 온프레미스 서버가 보내는 Pod CPU·메모리 표본: 받기·남기기·서비스 메트릭으로 읽기."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

import app.services.onprem_server_service as onprem_module
from app.core.exceptions import NotConfiguredError, UnauthorizedError
from app.dependencies import get_current_user, get_onprem_server_service
from app.enums import TargetKind
from app.main import app
from app.models.onprem_metric_sample import OnpremMetricSample
from app.models.project import Project
from app.models.service import Service
from app.models.user import User
from app.services.observability_service import ObservabilityService, bucket_server_metrics
from app.services.onprem_server_service import PodMetric
from tests.fakes_onprem import OWNER, FakeOnpremMetricSampleRepository, OnpremSetup

START = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def _sample(pod: str, offset_s: int, cpu: float, memory: int, service_id: int = 12):
    return OnpremMetricSample(
        service_id=service_id,
        pod=pod,
        collected_at=START + timedelta(seconds=offset_s),
        cpu_millicores=cpu,
        memory_bytes=memory,
    )


def _points(series) -> list[tuple[float, float]]:  # type: ignore[no-untyped-def]
    return [(p.timestamp - START.timestamp(), p.value) for p in series.points]


def test_bucket_total_averages_per_pod_then_sums_pods_with_prometheus_shape() -> None:
    samples = [
        _sample("a", 0, 100, 1000),
        _sample("a", 30, 300, 3000),
        _sample("b", 10, 500, 500),
        _sample("a", 70, 1000, 10),
    ]

    series = bucket_server_metrics(samples, START, 60, "total")

    assert [(s.metric, s.unit, s.pod) for s in series] == [
        ("cpu", "cores", None),
        ("memory", "bytes", None),
        ("network_receive", "bytes/s", None),
        ("network_transmit", "bytes/s", None),
    ]
    # 칸 0: a 평균 0.2 cores + b 0.5 cores, 칸 1: a 1.0 core
    assert _points(series[0]) == [(0.0, pytest.approx(0.7)), (60.0, pytest.approx(1.0))]
    assert _points(series[1]) == [(0.0, 2500.0), (60.0, 10.0)]
    assert series[2].points == [] and series[3].points == []


def test_bucket_by_pod_gives_cpu_and_memory_series_per_pod_sorted() -> None:
    samples = [_sample("b", 0, 500, 500), _sample("a", 0, 100, 1000)]

    series = bucket_server_metrics(samples, START, 60, "pod")

    assert [(s.metric, s.pod) for s in series] == [
        ("cpu", "a"),
        ("cpu", "b"),
        ("memory", "a"),
        ("memory", "b"),
    ]
    assert _points(series[0]) == [(0.0, pytest.approx(0.1))]


def _observability(
    *, has_server: bool, samples: FakeOnpremMetricSampleRepository | None = None
) -> ObservabilityService:
    services = AsyncMock()
    services.find_by_id_and_owner_id.return_value = object()
    services.search_target_ids_by_service_ids.return_value = {12: [7]}
    targets = AsyncMock()
    targets.search_by_ids.return_value = [
        SimpleNamespace(kind=TargetKind.ONPREM, onprem_server=object() if has_server else None)
    ]
    return ObservabilityService(
        services,
        AsyncMock(),
        None,
        None,
        target_repository=targets,
        metric_sample_repository=samples,  # type: ignore[arg-type]
    )


async def test_search_metrics_for_registered_server_reads_pushed_samples() -> None:
    samples = FakeOnpremMetricSampleRepository()
    samples.samples = [_sample("a", 0, 250, 2048), _sample("a", 30, 250, 2048, service_id=99)]
    service = _observability(has_server=True, samples=samples)
    namespace = await service.get_scope(OWNER, 12, 7)

    series = await service.search_metrics(7, namespace, START, START + timedelta(hours=1), 60)

    assert _points(series[0]) == [(0.0, pytest.approx(0.25))]
    assert _points(series[1]) == [(0.0, 2048.0)]
    service._client.search_metrics.assert_not_awaited()  # type: ignore[attr-defined]


async def test_search_metrics_for_shared_onprem_target_stays_not_configured() -> None:
    service = _observability(has_server=False)
    namespace = await service.get_scope(OWNER, 12, 7)

    with pytest.raises(NotConfiguredError):
        await service.search_metrics(7, namespace, START, START + timedelta(hours=1), 60)
    with pytest.raises(NotConfiguredError):
        await service.search_traffic_metrics(7, namespace, START, START + timedelta(hours=1), 60)


async def _server_with_service(setup: OnpremSetup) -> tuple[str, int, Service]:
    registration = await setup.service.create_server(OWNER, "home-lab")
    secret = await setup.connect(registration.registration_token, registration.server)
    project = await setup.projects.save(Project(name="p", owner_id=OWNER))
    service = await setup.services.save(
        Service(
            project_id=project.id,
            name="api",
            source_repository_url="https://github.com/o/r",
            github_installation_id=1,
            source_branch="main",
        )
    )
    await setup.services.replace_targets(service.id, {registration.server.target_id})
    return secret, registration.server.id, service


async def test_ingest_keeps_only_attached_service_namespaces_and_records_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(onprem_module, "_last_retention_at", None)
    setup = OnpremSetup()
    secret, _, service = await _server_with_service(setup)
    server = setup.servers.servers[0]
    server.last_seen_at = None

    await setup.service.ingest_metrics(
        secret,
        START,
        [
            PodMetric(f"svc-{service.id}", "app-a", 12.5, 1000),
            PodMetric("svc-999", "other", 1, 1),
            PodMetric("kube-system", "coredns", 1, 1),
            PodMetric(f"svc-{service.id}x", "bad", 1, 1),
        ],
    )

    assert [(s.service_id, s.pod, s.cpu_millicores) for s in setup.metrics.samples] == [
        (service.id, "app-a", 12.5)
    ]
    assert server.last_seen_at is not None
    assert len(setup.metrics.deleted_before) == 1


async def test_ingest_retention_runs_at_most_once_per_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(onprem_module, "_last_retention_at", None)
    setup = OnpremSetup()
    secret, _, _ = await _server_with_service(setup)

    await setup.service.ingest_metrics(secret, START, [])
    await setup.service.ingest_metrics(secret, START, [])

    assert len(setup.metrics.deleted_before) == 1


async def test_ingest_with_wrong_secret_is_unauthorized() -> None:
    setup = OnpremSetup()
    await _server_with_service(setup)

    with pytest.raises(UnauthorizedError):
        await setup.service.ingest_metrics("wrong", START, [])
    assert setup.metrics.samples == []


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[tuple[AsyncClient, OnpremSetup]]:
    setup = OnpremSetup()
    user = User(github_id=1, login="owner")
    user.id = OWNER
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_onprem_server_service] = lambda: setup.service
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        yield http, setup
    app.dependency_overrides.clear()


async def test_metrics_api_returns_204_and_stores_samples(
    client: tuple[AsyncClient, OnpremSetup],
) -> None:
    http, setup = client
    secret, _, service = await _server_with_service(setup)

    response = await http.post(
        "/api/v1/onprem-servers/metrics",
        headers={"Authorization": f"Bearer {secret}"},
        json={
            "collectedAt": "2026-10-04T12:00:00Z",
            "pods": [
                {
                    "namespace": f"svc-{service.id}",
                    "pod": "app-a",
                    "cpuMillicores": 12.5,
                    "memoryBytes": 123456,
                }
            ],
        },
    )

    assert response.status_code == 204
    assert setup.metrics.samples[0].memory_bytes == 123456


@pytest.mark.parametrize(
    "body",
    [
        {"collectedAt": "2026-10-04T12:00:00", "pods": []},
        {
            "collectedAt": "2026-10-04T12:00:00Z",
            "pods": [{"namespace": "svc-1", "pod": "p", "cpuMillicores": -1, "memoryBytes": 1}],
        },
        {
            "collectedAt": "2026-10-04T12:00:00Z",
            "pods": [
                {"namespace": "svc-1", "pod": f"p{i}", "cpuMillicores": 1, "memoryBytes": 1}
                for i in range(501)
            ],
        },
    ],
)
async def test_metrics_api_rejects_bad_input_with_422(
    client: tuple[AsyncClient, OnpremSetup], body: dict[str, object]
) -> None:
    http, setup = client
    secret, _, _ = await _server_with_service(setup)

    response = await http.post(
        "/api/v1/onprem-servers/metrics", headers={"Authorization": f"Bearer {secret}"}, json=body
    )

    assert response.status_code == 422


async def test_metrics_api_without_bearer_is_401(client: tuple[AsyncClient, OnpremSetup]) -> None:
    http, _ = client

    response = await http.post(
        "/api/v1/onprem-servers/metrics", json={"collectedAt": "2026-10-04T12:00:00Z", "pods": []}
    )

    assert response.status_code == 401
