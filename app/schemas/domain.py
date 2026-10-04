from pydantic import Field

from app.enums import TargetKind
from app.schemas.response import ApiModel
from app.services.domain_service import ServiceDomainDetail


class ServiceDomainResponse(ApiModel):
    """서비스가 한 타깃에서 열리는 공개 주소."""

    target_id: int
    target_name: str = Field(examples=["aws"])
    target_kind: TargetKind
    host: str | None = Field(
        default=None,
        description=(
            "`{서비스 이름}-{서비스 id}.{타깃 접미사}`. "
            "도메인 규칙이 없는 타깃과 관리형 DB 는 없다."
        ),
        examples=["my-app-12.likelion.uk"],
    )
    url: str | None = Field(
        default=None,
        description="`https://{host}`. host 가 없으면 없다.",
        examples=["https://my-app-12.likelion.uk"],
    )
    is_connected: bool = Field(
        description="배포가 성공해 주소로 접속할 수 있다. 첫 배포가 끝나기 전에는 false 다."
    )

    @classmethod
    def from_detail(cls, detail: ServiceDomainDetail) -> "ServiceDomainResponse":
        return cls(
            target_id=detail.target.id,
            target_name=detail.target.name,
            target_kind=detail.target.kind,
            host=detail.host,
            url=f"https://{detail.host}" if detail.host is not None else None,
            is_connected=detail.is_connected,
        )
