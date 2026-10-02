from pydantic import BaseModel


class BuildJobPayload(BaseModel):
    """BUILD job 의 입력. Worker 는 build_id 로 빌드·배포 요청·서비스를 읽는다."""

    build_id: int
