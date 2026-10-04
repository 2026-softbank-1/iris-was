from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, LargeBinary, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, now_utc

# chart 가 ConfigMap 하나에 담는다. ConfigMap 은 1 MiB 를 넘을 수 없다.
MAX_INIT_SCRIPT_BYTES = 1024 * 1024


class DatabaseInitScript(Base):
    """관리형 DB 초기화 스크립트 내용 1건. sha256 으로 찾는다(같은 내용은 한 번만 저장한다).

    Build Worker 가 분석할 때 풀어 둔 소스에서 읽어 sha256 을 다시 확인한 뒤 넣는다. 분석
    결과(`dependencies[].initScripts`)와 DB 서비스(`database_config.initScripts`)는 sha256 으로
    가리키기만 한다. 응답에는 내용을 내지 않고 Deploy Worker 만 values 로 옮긴다.
    """

    __tablename__ = "database_init_scripts"
    __table_args__ = (
        CheckConstraint(
            f"size_bytes >= 0 AND size_bytes <= {MAX_INIT_SCRIPT_BYTES}", name="size_bytes"
        ),
        CheckConstraint("octet_length(content) = size_bytes", name="content_size"),
    )

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    content: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=now_utc
    )
