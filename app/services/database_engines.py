"""관리형 DB 엔진 규칙: 고정 이미지·포트·자격 증명 변수 이름·연결 정보.

자격 증명은 DB 서비스의 암호화 변수(POSTGRES_PASSWORD 등)로 둔다. 그 변수는 플랫폼이 관리해 변수 API
에서 고칠 수 없고(`managed_variable_keys`), 비밀번호는 어떤 응답에도 평문으로 나가지 않는다.
연결 정보(url·host·…)는 참조 변수가 배포 직전에 이 모듈로 계산한다.
"""

import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass

from app.core.exceptions import InvalidInputError
from app.enums import DatabaseEngine, ReferenceProperty

# 응답·로그에서 비밀번호 자리에 쓰는 값.
MASK = "****"
_PASSWORD_BYTES = 24
DEFAULT_STORAGE_GI = 5
MIN_STORAGE_GI = 1
MAX_STORAGE_GI = 20
_IDENTIFIER = re.compile(r"[^a-z0-9_]+")
# 이미지 참조는 `repo:tag@sha256:…` 다. repo 는 docker.io/library/{엔진 공식 이미지} 로만 받는다.
_IMAGE_REF = re.compile(
    r"^(?P<repository>docker\.io/library/[a-z0-9]+):(?P<tag>[A-Za-z0-9._-]+)@(?P<digest>sha256:[a-f0-9]{64})$"
)


@dataclass(frozen=True)
class EngineSpec:
    engine: DatabaseEngine
    repository: str
    default_image: str
    port: int
    url_scheme: str
    # 연결 URL 로 받는 스킴(SCHEME_MISMATCH 검사). 드라이버 접미사(+asyncpg 등)는 앞부분만 본다.
    accepted_schemes: frozenset[str]
    user_key: str | None
    password_key: str
    database_key: str | None
    # 그 밖에 플랫폼이 만드는 변수(값을 응답에 내지 않는다).
    extra_secret_keys: tuple[str, ...] = ()

    @property
    def has_user(self) -> bool:
        return self.user_key is not None

    @property
    def has_database(self) -> bool:
        return self.database_key is not None

    @property
    def managed_keys(self) -> frozenset[str]:
        keys = {self.password_key, *self.extra_secret_keys}
        if self.user_key:
            keys.add(self.user_key)
        if self.database_key:
            keys.add(self.database_key)
        return frozenset(keys)

    @property
    def secret_keys(self) -> frozenset[str]:
        return frozenset({self.password_key, *self.extra_secret_keys})

    @property
    def properties(self) -> list[ReferenceProperty]:
        properties = [ReferenceProperty.URL, ReferenceProperty.HOST, ReferenceProperty.PORT]
        properties.append(ReferenceProperty.USER)
        properties.append(ReferenceProperty.PASSWORD)
        if self.has_database:
            properties.append(ReferenceProperty.DATABASE)
        return properties


# digest 는 2026-10-04 에 공식 이미지 index(멀티 아키텍처)에서 고정했다. 설정 DATABASE_IMAGES 로
# 바꾼다.
ENGINE_SPECS: Mapping[DatabaseEngine, EngineSpec] = {
    DatabaseEngine.POSTGRES: EngineSpec(
        engine=DatabaseEngine.POSTGRES,
        repository="docker.io/library/postgres",
        default_image=(
            "docker.io/library/postgres:16-alpine"
            "@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"
        ),
        port=5432,
        url_scheme="postgresql",
        accepted_schemes=frozenset({"postgres", "postgresql"}),
        user_key="POSTGRES_USER",
        password_key="POSTGRES_PASSWORD",
        database_key="POSTGRES_DB",
    ),
    DatabaseEngine.MYSQL: EngineSpec(
        engine=DatabaseEngine.MYSQL,
        repository="docker.io/library/mysql",
        default_image=(
            "docker.io/library/mysql:8.4"
            "@sha256:6ea90827b1100f8f2ae306a539f86d2c264a26ed435a2a9f75551dd5c3aeb242"
        ),
        port=3306,
        url_scheme="mysql",
        accepted_schemes=frozenset({"mysql", "mysql2", "mariadb"}),
        user_key="MYSQL_USER",
        password_key="MYSQL_PASSWORD",
        database_key="MYSQL_DATABASE",
        extra_secret_keys=("MYSQL_ROOT_PASSWORD",),
    ),
    DatabaseEngine.MONGODB: EngineSpec(
        engine=DatabaseEngine.MONGODB,
        repository="docker.io/library/mongo",
        default_image=(
            "docker.io/library/mongo:7"
            "@sha256:1f995ad6fdb93244a1addab1b58f934a0bc2f5643c38e02f5e9d7f0c7d227a7b"
        ),
        port=27017,
        url_scheme="mongodb",
        accepted_schemes=frozenset({"mongodb", "mongodb+srv"}),
        user_key="MONGO_INITDB_ROOT_USERNAME",
        password_key="MONGO_INITDB_ROOT_PASSWORD",
        database_key="MONGO_INITDB_DATABASE",
    ),
    DatabaseEngine.REDIS: EngineSpec(
        engine=DatabaseEngine.REDIS,
        repository="docker.io/library/redis",
        default_image=(
            "docker.io/library/redis:7-alpine"
            "@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499"
        ),
        port=6379,
        url_scheme="redis",
        accepted_schemes=frozenset({"redis", "rediss"}),
        # redis 는 사용자 변수가 없다. 연결 정보의 user 는 ACL 기본 사용자 `default` 다.
        user_key=None,
        password_key="REDIS_PASSWORD",
        database_key=None,
    ),
}
_REDIS_USER = "default"


@dataclass(frozen=True)
class ImageRef:
    repository: str
    tag: str
    digest: str

    @property
    def reference(self) -> str:
        return f"{self.repository}:{self.tag}@{self.digest}"


def get_engine_spec(engine: DatabaseEngine | str) -> EngineSpec:
    return ENGINE_SPECS[DatabaseEngine(engine)]


def parse_image_ref(value: str) -> ImageRef:
    match = _IMAGE_REF.fullmatch(value.strip())
    if match is None:
        raise InvalidInputError("database image must be repo:tag@sha256 digest", field="image")
    return ImageRef(match["repository"], match["tag"], match["digest"])


def resolve_image(engine: DatabaseEngine, overrides: Mapping[str, str]) -> ImageRef:
    """설정 override 가 있으면 그 이미지, 없으면 고정 기본값. 엔진 공식 리포지토리만 받는다."""
    spec = get_engine_spec(engine)
    image = parse_image_ref(overrides.get(engine.value) or spec.default_image)
    if image.repository != spec.repository:
        raise InvalidInputError(
            "database image must be the official engine image", field="image", engine=engine
        )
    return image


def normalize_identifier(value: str | None, default: str) -> str:
    """DB 사용자·데이터베이스 이름. 영문 소문자·숫자·밑줄, 문자로 시작, 32자 이하다."""
    cleaned = _IDENTIFIER.sub("_", (value or "").strip().lower()).strip("_")[:32]
    if not cleaned or not cleaned[0].isalpha():
        return default
    if cleaned == "root":
        # mysql 은 MYSQL_USER=root 를 거절한다.
        return default
    return cleaned


def generate_credentials(
    engine: DatabaseEngine, user: str | None, database: str | None
) -> dict[str, str]:
    """DB 서비스 변수로 저장할 자격 증명. 비밀번호는 매번 새로 만든다(로그·응답에 남기지 않는다)."""
    spec = get_engine_spec(engine)
    values: dict[str, str] = {spec.password_key: secrets.token_urlsafe(_PASSWORD_BYTES)}
    if spec.user_key:
        values[spec.user_key] = normalize_identifier(user, "app")
    if spec.database_key:
        values[spec.database_key] = normalize_identifier(database, "app")
    for key in spec.extra_secret_keys:
        values[key] = secrets.token_urlsafe(_PASSWORD_BYTES)
    return values


@dataclass(frozen=True)
class DatabaseConnection:
    engine: DatabaseEngine
    host: str
    port: int
    user: str
    password: str
    database: str | None

    def url(self, *, masked: bool = False) -> str:
        spec = get_engine_spec(self.engine)
        password = MASK if masked else self.password
        if self.engine == DatabaseEngine.REDIS:
            return f"{spec.url_scheme}://{self.user}:{password}@{self.host}:{self.port}"
        base = f"{spec.url_scheme}://{self.user}:{password}@{self.host}:{self.port}"
        path = f"/{self.database}" if self.database else ""
        if self.engine == DatabaseEngine.MONGODB:
            # 루트 사용자는 admin DB 에 만들어진다.
            return f"{base}{path}?authSource=admin"
        return f"{base}{path}"

    def property(self, name: ReferenceProperty, *, masked: bool = False) -> str | None:
        """None 이면 이 엔진에 없는 속성이다."""
        match name:
            case ReferenceProperty.URL:
                return self.url(masked=masked)
            case ReferenceProperty.HOST:
                return self.host
            case ReferenceProperty.PORT:
                return str(self.port)
            case ReferenceProperty.USER:
                return self.user
            case ReferenceProperty.PASSWORD:
                return MASK if masked else self.password
            case ReferenceProperty.DATABASE:
                return self.database


def build_connection(
    engine: DatabaseEngine, host: str, port: int, variables: Mapping[str, str]
) -> DatabaseConnection:
    """DB 서비스의 (복호화한) 변수에서 연결 정보를 만든다. 비밀번호가 없으면 빈 값이다."""
    spec = get_engine_spec(engine)
    user = variables.get(spec.user_key, "") if spec.user_key else _REDIS_USER
    database = variables.get(spec.database_key) if spec.database_key else None
    return DatabaseConnection(
        engine=engine,
        host=host,
        port=port,
        user=user,
        password=variables.get(spec.password_key, ""),
        database=database,
    )


def url_template(engine: DatabaseEngine, host: str, port: int, config: Mapping[str, object]) -> str:
    """비밀번호를 가린 연결 문자열. 서비스 응답의 connection.urlTemplate 이다."""
    spec = get_engine_spec(engine)
    user = str(config.get("user") or "") if spec.has_user and spec.user_key else _REDIS_USER
    database = str(config.get("database") or "") if spec.has_database else None
    return DatabaseConnection(engine, host, port, user, "", database).url(masked=True)
