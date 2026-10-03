import pytest
from botocore.stub import Stubber

from app.clients.aws_clients import CloudWatchBuildLogClient, LogLine
from app.core.exceptions import ExternalError, InvalidInputError

GROUP = "/aws/codebuild/iris-dev-build"
STREAM = "7c1e0f2a-uuid"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> CloudWatchBuildLogClient:
    # 스텁이 요청을 가로채 서명까지 가지 않지만 boto3 가 자격 증명을 찾지 않도록 넣어 둔다.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    return CloudWatchBuildLogClient.create_reader("ap-northeast-2")


def _params(limit: int = 100, next_token: str | None = None) -> dict[str, object]:
    params: dict[str, object] = {
        "logGroupName": GROUP,
        "logStreamName": STREAM,
        "startFromHead": True,
        "limit": limit,
    }
    if next_token is not None:
        params["nextToken"] = next_token
    return params


def test_create_reader_does_not_use_the_s3_signing_config(
    client: CloudWatchBuildLogClient,
) -> None:
    assert client._client.meta.config.signature_version != "s3v4"


async def test_read_events_reads_from_head_and_strips_trailing_newlines(
    client: CloudWatchBuildLogClient,
) -> None:
    with Stubber(client._client) as stubber:
        stubber.add_response(
            "get_log_events",
            {
                "events": [
                    {"timestamp": 1_000, "message": "[Container] start\n"},
                    {"timestamp": 2_000, "message": "second\r\n"},
                ],
                "nextForwardToken": "f/2",
            },
            _params(),
        )

        chunk = await client.read_events(GROUP, STREAM, 100, None)

    assert chunk.lines == [LogLine(1_000, "[Container] start"), LogLine(2_000, "second")]
    assert chunk.next_token == "f/2"


async def test_read_events_continues_from_next_token(client: CloudWatchBuildLogClient) -> None:
    with Stubber(client._client) as stubber:
        stubber.add_response(
            "get_log_events", {"events": [], "nextForwardToken": "f/2"}, _params(10, "f/2")
        )

        chunk = await client.read_events(GROUP, STREAM, 10, "f/2")

    assert chunk.lines == []
    assert chunk.next_token == "f/2"


async def test_read_events_missing_stream_returns_empty_chunk_with_same_token(
    client: CloudWatchBuildLogClient,
) -> None:
    with Stubber(client._client) as stubber:
        stubber.add_client_error("get_log_events", "ResourceNotFoundException")

        chunk = await client.read_events(GROUP, STREAM, 100, "f/1")

    assert chunk.lines == []
    assert chunk.next_token == "f/1"


async def test_read_events_invalid_token_raises_invalid_input(
    client: CloudWatchBuildLogClient,
) -> None:
    with Stubber(client._client) as stubber:
        stubber.add_client_error("get_log_events", "InvalidParameterException")

        with pytest.raises(InvalidInputError):
            await client.read_events(GROUP, STREAM, 100, "garbage")


async def test_read_events_aws_failure_is_external_error_without_details(
    client: CloudWatchBuildLogClient,
) -> None:
    with Stubber(client._client) as stubber:
        stubber.add_client_error(
            "get_log_events", "AccessDeniedException", "role arn:aws:iam::1:role/secret denied"
        )

        with pytest.raises(ExternalError) as error:
            await client.read_events(GROUP, STREAM, 100, None)

    assert "secret" not in str(error.value)
