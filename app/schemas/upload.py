from datetime import datetime
from typing import Self

from pydantic import Field

from app.models.service_upload import ServiceUpload
from app.schemas.response import ApiModel


class UploadResponse(ApiModel):
    """올라온 소스 아카이브. `uploadId` 로 `CLI` 배포 요청을 만든다."""

    upload_id: str = Field(
        description="추측할 수 없는 업로드 ID. 배포 요청 하나에만 쓸 수 있다.",
        examples=["Zq3h8mP0xYkq2oVd1sT6uXn4wJc9bLrE5aGfHiK7yMA"],
    )
    size_bytes: int = Field(description="올라온 아카이브(gzip)의 바이트 수", examples=[123456])
    sha256: str = Field(
        description="올라온 아카이브의 sha256(hex). 클라이언트가 보낸 값과 맞춰 볼 수 있다.",
        examples=["9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"],
    )
    expires_at: datetime = Field(description="이 시각이 지나면 배포 요청에 쓸 수 없다.")

    @classmethod
    def from_model(cls, upload: ServiceUpload) -> Self:
        return cls(
            upload_id=upload.public_id,
            size_bytes=upload.size_bytes,
            sha256=upload.sha256,
            expires_at=upload.expires_at,
        )
