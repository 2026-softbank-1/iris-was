# Python 백엔드 코드 컨벤션 가이드

- **적용 대상**: FastAPI 기반 Python 백엔드 프로젝트 전반
- **스택 기준**: Python 3.13+, FastAPI, Pydantic v2, SQLAlchemy 2.0(async)
- **전제**: 프로젝트에 용어 사전·필드 표준 문서가 있다면 **그 표준을 최우선**으로 따른다. 이 가이드는 그 위의 공통 규칙이다.

> **핵심 철학**
>
> 1. **네이밍으로 의도를 전달한다.** 주석은 "왜(rationale)" 와 "외부 계약" 에만 단다.
> 2. **레이어마다 접미사를 고정한다.** 이름만 보고 어느 계층인지 안다.
> 3. **읽기와 쓰기를 이름으로 구분한다.**
> 4. **변환(매핑) 지점을 한곳에 모은다.**
> 5. **계층 간 경계를 섞지 않는다.** 각 레이어는 자기 타입만 다룬다.

---

## 목차

1. 일반 Python 네이밍 원칙
2. 레이어별 컨벤션
  - 2.1 API (Router)
  - 2.2 DTO (Pydantic Schema)
  - 2.3 Service
  - 2.4 Client (외부 API)
  - 2.5 DB (Model · Repository)
  - 2.6 변환 (Schema ↔ Model ↔ Service 입출력)
  - 2.7 변수명 스타일
3. 디렉토리·파일 구조
4. 타입 힌트 · 데코레이터 · 비동기 관례
5. 에러 처리와 예외
6. 로깅
7. 설정과 시크릿
8. 날짜·시간
9. 페이지네이션
10. 테스트
11. 린터 · 포매터 · 타입체크
12. 주석 규칙
13. 신규 코드 작성 체크리스트

- 부록 A. 레이어별 네이밍 한눈 요약

> 예시에 쓰인 `User`, `Order` 등은 설명용 가상 도메인이다. 실제 프로젝트의 도메인 이름으로 바꿔 적용한다.

---

## 1. 일반 Python 네이밍 원칙


| 대상               | 규칙                               | 예                                                     |
| ---------------- | -------------------------------- | ----------------------------------------------------- |
| 클래스 / 모델 / 스키마   | `PascalCase`                     | `OrderService`, `CreateOrderRequest`, `PaymentClient` |
| 함수 / 변수 / 속성     | `snake_case`                     | `place_order()`, `created_at`                         |
| 상수 / 모듈 레벨 설정    | `UPPER_SNAKE_CASE`               | `MAX_RETRY_COUNT`, `DEFAULT_PAGE_SIZE`                |
| 모듈 / 패키지(디렉토리)   | 소문자 `snake_case`. 케밥·대문자 금지      | `order_service.py`, `app/clients/`                    |
| Enum 클래스 / 멤버    | 클래스 `PascalCase`, 멤버는 저장용 코드 문자열 | `class OrderStatus(StrEnum): PAID = "paid"`           |
| 타입 별칭(TypeAlias) | `PascalCase`                     | `OrderId = str`                                       |
| 비공개(모듈/클래스 내부)   | 선행 언더스코어 1개                      | `_build_query()`, `_session`                          |
| 테스트 함수           | `test_{대상}_{시나리오}`               | `test_place_order_returns_order()`                    |


### 1.1 약어 처리 규칙

- **클래스명(PascalCase)**: 글자 수와 무관하게 약어는 **첫 글자만 대문자**. 2글자 약어(`ID`·`DB`·`IO`)도 동일하게 `Id`·`Db`·`Io`.
  - 좋은 예: `HttpClient`, `JsonEncoder`, `OrderId`, `DbClient`, `IoError`
  - 나쁜 예: ~~`HTTPClient`~~, ~~`JSONEncoder`~~, ~~`OrderID`~~, ~~`DBClient`~~
- **변수/필드(snake_case)**: 전부 소문자. camelCase 금지.
  - 좋은 예: `order_id`, `user_id`, `created_at`
  - 나쁜 예: ~~`orderID`~~, ~~`userId`~~

### 1.2 Boolean 접두어

상태를 이름만으로 알 수 있게 `is_` / `has_` / `can_` / `should_` 를 붙인다.


| 접두어       | 예                                    |
| --------- | ------------------------------------ |
| `is_`     | `is_active`, `is_paid`, `is_deleted` |
| `has_`    | `has_items`, `has_permission`        |
| `can_`    | `can_cancel`, `can_refund`           |
| `should_` | `should_retry`, `should_notify`      |


```python
# 나쁜 예 — Boolean 인지 이름으로 알 수 없음
def active(user: User) -> bool: ...
def items(order: Order) -> bool: ...
```

### 1.3 함수 동사 — get / find / search / check 구분

**반환 형태**로 동사를 고정한다. 레이어 전반(Service·Repository)에 일관 적용한다.


| 접두어 | 의미 | 반환 |
|---|---|---|
| `get_*` | 반드시 1건. 없으면 예외 | `T` |
| `find_*` | 있을 수도 없을 수도 | `T \| None` |
| `search_*` / `find_all_*` | 0건 이상 다건 | `list[T]` / `Page[T]` |
| `check_*` / `validate_*` | 검증. Boolean 반환 또는 예외 | `bool` / `None`(raise) |
| `create_* / update_* / delete_*` | 상태 변경(쓰기) | 결과 객체 |


```python
async def get_order(self, order_id: str) -> Order: ...          # 없으면 raise
async def find_order(self, order_id: str) -> Order | None: ...  # nullable
async def search_orders(self, f: OrderFilter) -> list[Order]: ...
```

> 프로젝트 고유 동작은 그 동작을 그대로 동사로 쓴다(예: `place_order`, `cancel_order`). 위 표는 조회·검증의 공통 규칙이다.

---

## 2. 레이어별 컨벤션

일반적인 요청 흐름:

```text
HTTP ──> Router ──> Service ──> Repository ──> DB(Model)
                       │
                       └──> Client (외부 API)
        Pydantic Schema(요청/응답)        SQLAlchemy Model(영속)
```

### 2.1 API (Router)

FastAPI 는 클래스 컨트롤러 대신 `APIRouter` 를 쓴다. **도메인 단위로 라우터 파일을 나누고**, 엔드포인트 함수는 동사로 시작한다.


| 대상        | 규칙                                       | 예                                                             |
| --------- | ---------------------------------------- | ------------------------------------------------------------- |
| 라우터 객체    | 파일당 1개, 이름은 `router`                     | `router = APIRouter(prefix="/api/v1/orders", tags=["order"])` |
| 라우터 파일    | `{domain}_router.py`                     | `order_router.py`, `user_router.py`                           |
| 엔드포인트 함수  | `snake_case` 동사 시작. URL 이 아니라 **동작**을 표현 | `create_order`, `search_orders`, `cancel_order`               |
| 경로 prefix | `/api/v{n}/{domain-plural}` (kebab 복수)   | `/api/v1/orders`, `/api/v1/users`                             |


```python
# order_router.py
router = APIRouter(prefix="/api/v1/orders", tags=["order"])

@router.get("", response_model=list[OrderResponse])
async def search_orders(
    filter_: OrderFilter = Depends(),
    service: OrderService = Depends(get_order_service),
) -> list[OrderResponse]:
    orders = await service.search_orders(filter_)
    return [OrderResponse.from_model(o) for o in orders]

@router.post("/{order_id}/cancel", response_model=OrderResponse)
async def cancel_order(
    order_id: str,
    service: OrderService = Depends(get_order_service),
) -> OrderResponse: ...
```

**라우터 규칙**

- 라우터는 **얇게**. 비즈니스 로직 없이 Service 호출 + Schema 변환만.
- `response_model` 을 항상 명시해 응답 스키마를 고정한다.
- 의존성 주입은 `Depends(get_xxx_service)` 로. 직접 인스턴스화 금지.
- 예약어 충돌(`filter`, `id`, `type`)은 후행 언더스코어(`filter_`, `type_`)로 회피.

### 2.2 DTO (Pydantic Schema)

요청/응답은 Pydantic `BaseModel`. **명사가 아니라 동작+대상** 으로 이름 짓는다.


| 종류    | 패턴                                              | 예                                          |
| ----- | ----------------------------------------------- | ------------------------------------------ |
| 쓰기 요청 | `{Action}{Domain}Request`                       | `CreateOrderRequest`, `CancelOrderRequest` |
| 조회 필터 | `{Domain}Filter` (쿼리 파라미터 묶음)                   | `OrderFilter`                              |
| 응답    | `{Domain}Response` / `{Action}{Domain}Response` | `OrderResponse`, `CreateOrderResponse`     |
| 내부 중첩 | 바깥 스키마 내부 클래스 또는 `{Domain}{Part}Schema`         | `OrderResponse.LineItem`                   |


```python
class CreateOrderRequest(BaseModel):
    user_id: str
    items: list[LineItemRequest]

class OrderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)  # ORM 모델 → 스키마 변환 허용

    id: str
    user_id: str
    status: OrderStatus
    total_amount: int
    created_at: datetime
```

**Schema 규칙**

- **필드명은 프로젝트 표준(`snake_case`)** 을 그대로 쓴다. 외부(예: 프론트 camelCase)로 내보낼 때만 `alias` + `populate_by_name` 으로 변환한다 — 서버 내부는 항상 snake_case.
- 요청 스키마와 응답 스키마를 **분리**한다. 하나의 모델을 입출력에 겸용 금지(노출 필드가 다름).
- 응답 스키마에 ORM 모델을 직접 넣지 않는다. `from_attributes=True` + 변환 메서드(§2.6)로 경계를 끊는다.
- Enum 타입은 한곳에서 정의한 `StrEnum` 을 공유한다(문자열 코드 저장).

### 2.3 Service

유스케이스를 조율하는 계층. 비즈니스 로직의 중심이다.


| 대상           | 규칙                | 예                                               |
| ------------ | ----------------- | ----------------------------------------------- |
| 클래스          | `{Domain}Service` | `OrderService`, `UserService`                   |
| 특정 책임 전담 서비스 | `{목적}Service`     | `PaymentService`, `NotificationService`         |
| 메서드          | 동사 시작(§1.3)       | `create_order`, `cancel_order`, `search_orders` |


```python
class OrderService:
    def __init__(
        self,
        order_repository: OrderRepository,
        payment_service: PaymentService,
        notification_service: NotificationService,
    ) -> None:
        self._order_repository = order_repository
        self._payment_service = payment_service
        self._notification_service = notification_service

    async def cancel_order(self, order_id: str) -> Order:
        order = await self._order_repository.get_by_id(order_id)   # 없으면 raise
        await self._payment_service.refund(order)
        order.cancel()
        # ... 후속 처리 (await 순차 실행)
        return order
```

**Service 규칙**

- 의존성은 생성자 주입, 내부 보관은 `self._name` (비공개). 축약 금지(`self._svc` ✗).
- 한 Service 가 너무 커지면 **책임을 별도 서비스로 분리**(`PaymentService`, `NotificationService` 등). 단일 책임 유지.
- DB 트랜잭션 경계는 Service 메서드에서 관리(`async with session.begin()`), Repository/Model 은 트랜잭션을 모른다.
- 외부 호출은 반드시 Client 를 통해서. Service 가 `httpx` 등을 직접 부르지 않는다.

### 2.4 Client (외부 API)

외부 시스템(결제 PG, 메일, LLM, 타 서비스 등) 호출. **추상 인터페이스 + 구현 클래스** 로 표현해 교체·폴백·테스트를 쉽게 한다.


| 대상                  | 규칙                                           | 예                                                  |
| ------------------- | -------------------------------------------- | -------------------------------------------------- |
| 인터페이스(Protocol/ABC) | `{역할}Client`                                 | `PaymentClient`, `EmailClient`, `LlmClient`        |
| 구현                  | `{Provider}{역할}Client` 또는 `{Provider}Client` | `TossPaymentClient`, `SesEmailClient`, `GptClient` |
| 메서드                 | 외부 동작 동사                                     | `charge`, `send_email`, `generate_completion`      |


```python
class PaymentClient(Protocol):
    async def charge(self, amount: int, token: str) -> ChargeResult: ...
    async def refund(self, transaction_id: str) -> RefundResult: ...

class TossPaymentClient:                 # PaymentClient 구현
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def charge(self, amount: int, token: str) -> ChargeResult:
        res = await self._http.post("/payments", json={...})   # 반드시 Async 클라이언트
        return ChargeResult.from_response(res.json())
```

**Client 규칙**

- **반드시 비동기 클라이언트**(`httpx.AsyncClient` 등)를 쓴다. 동기 SDK 를 `async def` 안에서 호출하면 이벤트 루프가 막힌다. 동기 라이브러리뿐이면 `await asyncio.to_thread(...)` 로 감싼다.
- 인터페이스(Protocol)에 의존하고 구현을 주입한다 → 프로바이더 교체·폴백·테스트가 쉬움.
- 외부 응답 모델(SDK 타입)을 Service 위로 새지 않게 한다. Client 가 우리 도메인 타입으로 변환해 반환한다.
- 재시도·타임아웃·인증은 Client 내부에 둔다.

### 2.5 DB (Model · Repository)

#### SQLAlchemy Model


| 대상     | 규칙                                   | 예                                              |
| ------ | ------------------------------------ | ---------------------------------------------- |
| 모델 클래스 | `PascalCase` 단수                      | `Order`, `User`, `LineItem`                    |
| 테이블명   | `__tablename__` 에 `snake_case` 단수 명시 | `"order"`, `"user"`, `"line_item"`             |
| 컬럼     | 프로젝트 `snake_case` 표준                 | `created_at`, `total_amount`, `status`         |
| 상태 전이  | setter 금지. 의도 동사 메서드                 | `mark_as_paid()`, `cancel()`, `add_item(item)` |


```python
class Order(Base):
    __tablename__ = "order"

    id: Mapped[str] = mapped_column(primary_key=True)
    user_id: Mapped[str]
    status: Mapped[OrderStatus] = mapped_column(default=OrderStatus.PENDING)
    total_amount: Mapped[int]
    created_at: Mapped[datetime]
    paid_at: Mapped[datetime | None]

    def mark_as_paid(self, paid_at: datetime) -> None:   # 상태 전이 = 동사 메서드
        self.status = OrderStatus.PAID
        self.paid_at = paid_at
```

#### Repository

DB 접근 전담. **메서드 네이밍은 §1.3 의 get/find/search 규칙** 을 그대로 적용한다.


| 반환 | 패턴 | 예 |
|---|---|---|
| 단일(없으면 예외) | `get_by_{key}` | `get_by_id(order_id) -> Order` |
| 단일 nullable | `find_by_{key}` | `find_by_user_id(user_id) -> Order \| None` |
| 다건 | `search_by_{cond}` / `find_all_by_{cond}` | `search_by_status(status) -> list[Order]` |
| 저장 | `save` / `add` | `save(order) -> Order` |


```python
class OrderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, order_id: str) -> Order:
        order = await self._session.get(Order, order_id)
        if order is None:
            raise OrderNotFoundError(order_id)
        return order

    async def find_by_user_id(self, user_id: str) -> Order | None: ...

    async def search_by_status(self, status: OrderStatus) -> list[Order]:
        stmt = select(Order).where(Order.status == status)
        return list((await self._session.scalars(stmt)).all())
```

**DB 규칙**

- Repository 는 **Model 만 반환**한다(Pydantic 스키마로 변환하지 않음 — 그건 Router 의 일). 경계를 섞지 않는다.
- 비즈니스 로직(분기·계산) 금지. 순수 조회/저장만.
- `session` 은 주입받는다. Repository 가 세션을 생성·커밋하지 않는다(트랜잭션은 Service).
- **소프트 삭제를 사용한다.** 물리 삭제 대신 `is_deleted: bool`(기본 `False`) + `deleted_at: datetime | None` 을 믹스인으로 공유하고, 조회는 기본적으로 `is_deleted == False` 만 본다. 공통 컬럼(`created_at`, `updated_at`)도 같은 `Base` 믹스인에 둔다.

### 2.6 변환 (Schema ↔ Model ↔ Service 입출력)

Python 에는 확장 함수가 없으므로 **classmethod / 인스턴스 메서드** 로 변환 지점을 고정한다.


| 방향                     | 패턴                                                | 위치          |
| ---------------------- | ------------------------------------------------- | ----------- |
| Model → 응답 Schema      | `Response.from_model(model)` classmethod          | 응답 스키마      |
| 요청 Schema → Model/입력객체 | `request.to_model()` 또는 `Order.from_request(req)` | 요청 스키마 / 모델 |
| 외부 SDK 응답 → 도메인 타입     | Client 내부 변환 함수                                   | Client      |


```python
class OrderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    user_id: str
    status: OrderStatus

    @classmethod
    def from_model(cls, order: Order) -> "OrderResponse":
        return cls.model_validate(order)
```

**변환 규칙**

- 변환은 **한 방향만**. Model 이 Schema 를 import 하면 의존 방향이 깨진다(리뷰에서 차단).
- 매핑이 단순하면 `model_validate`(`from_attributes`)로 충분. 가공이 필요하면 명시적 classmethod.
- 전역 `Mapper` 유틸 클래스 금지. 변환은 대상 타입에 붙인다(탐색·응집 측면에서 유리).

### 2.7 변수명 스타일


| 규칙                       | 좋은 예                                         | 나쁜 예                          |
| ------------------------ | -------------------------------------------- | ----------------------------- |
| 주입 의존성: 비공개 + 풀네임        | `self._order_repository`                     | ~~`self._repo`~~              |
| 컬렉션: 복수형                 | `orders: list[Order]`, `items`               | ~~`order_list`~~, ~~`data`~~  |
| dict: `{value}_by_{key}` | `status_by_order_id: dict[str, OrderStatus]` | ~~`map`~~, ~~`d`~~            |
| Boolean 변수               | `is_paid`, `has_items`                       | ~~`paid`~~, ~~`flag`~~        |
| 중간 변수: 의미 중심             | `charge_result`, `refund_amount`             | ~~`tmp`~~, ~~`res`~~, ~~`x`~~ |
| 예약어 회피                   | `id_`, `type_`, `filter_`                    | (충돌 무시)                       |


---

## 3. 디렉토리·파일 구조

**레이어 우선 구조**를 따른다. 레이어별 디렉토리(`routers/`·`services/`·`repositories/`…)로 나눈다.

```text
app/
├─ main.py                      # FastAPI 앱 생성, 라우터 등록
├─ enums.py                     # StrEnum 정의 한곳 (레이어 중립 — Model·Schema 공용)
├─ routers/
│   └─ order_router.py
├─ schemas/                     # Pydantic (요청/응답/필터)
│   └─ order.py
├─ services/
│   └─ order_service.py
├─ clients/                     # 외부 API
│   └─ payment_client.py        # PaymentClient(Protocol) + 구현
├─ repositories/
│   └─ order_repository.py
├─ models/                      # SQLAlchemy
│   ├─ base.py
│   └─ order.py
├─ dependencies.py              # Depends 주입 함수(get_order_service 등)
└─ core/
    ├─ config.py                # 환경설정(Pydantic Settings)
    └─ exceptions.py            # 도메인 예외
```

**규칙**

- **한 파일 = 한 책임.** Model·Repository·Service 는 각각 다른 파일. 단, 한 도메인의 여러 Pydantic 스키마(요청·응답·필터)는 한 파일에 모아도 된다.
- 디렉토리·파일명은 소문자 `snake_case`. `_router`/`_service`/`_repository` 접미사로 검색성을 높인다.
- Enum 은 한곳에서 정의하고 Model·Schema 가 공유한다(중복 정의 금지). **위치는 레이어 중립**(`app/enums.py`) — `schemas/` 안에 두면 Model 이 Enum 을 쓸 때 Model→Schema 역의존이 생겨 §2.6 규칙을 어긴다.

---

## 4. 타입 힌트 · 데코레이터 · 비동기 관례

### 4.1 타입 힌트

- **모든 함수 시그니처에 타입 힌트** 를 단다(인자·반환). 반환 없으면 `-> None`.
- Python 3.10+ 문법: `str | None`, `list[Order]`, `dict[str, int]`. `Optional`/`List` 대신 신문법.
- 공개 API 는 구체 타입, 내부 유연성이 필요하면 `Protocol` 로 인터페이스 정의.

### 4.2 데코레이터


| 데코레이터                                   | 용도                              |
| --------------------------------------- | ------------------------------- |
| `@router.{method}(...)`                 | 엔드포인트 등록. `response_model` 명시   |
| `@classmethod`                          | 변환 생성자(`from_model`)            |
| `@property`                             | 파생 읽기 값(`is_paid`) — 부수효과 없을 때만 |
| `@field_validator` / `@model_validator` | Pydantic 검증                     |
| `@lru_cache`                            | 설정·싱글턴 의존성 캐싱                   |


### 4.3 비동기

- I/O(외부 API·DB)는 전부 `async def` + `await`. 한 요청 안에서 `await` 는 위→아래 순차 실행된다.
- **동기 블로킹 라이브러리를 `async def` 안에서 직접 호출 금지.** 비동기 클라이언트를 쓰거나 `await asyncio.to_thread(fn, ...)` 로 감싼다.
- 응답을 먼저 반환하고 무거운 작업을 뒤로 미루려면 `BackgroundTasks` 또는 작업 큐(Arq/Celery)를 쓴다.
- 독립적인 여러 외부 호출은 `asyncio.gather(...)` 로 병렬화한다(순차 `await` 누적 금지).

---

## 5. 에러 처리와 예외

도메인 의미를 담은 **예외를 던지고**, HTTP 변환은 한곳(핸들러)에서 한다.

| 규칙 | 내용 |
|---|---|
| 예외 네이밍 | `{Domain}{사유}Error` (예: `OrderNotFoundError`, `PaymentFailedError`) |
| 공통 베이스 | 모든 도메인 예외는 `AppError` 하나를 상속. 카테고리(`NotFoundError`/`ValidationError`/`ConflictError`/`ExternalError`)를 중간에 둔다 |
| raise 위치 | Repository·Service 에서 도메인 예외를 raise. Router 는 `try/except` 하지 않는다 |
| HTTP 매핑 | FastAPI `exception_handler` 한곳에서 예외 → 상태코드 변환 |
| 컨텍스트 | 예외에 식별자를 담는다(`OrderNotFoundError(order_id)`). 메시지에 민감정보 금지 |
| 금지 | 빈 `except:` / 광범위 `except Exception` 으로 삼키기 / 예외를 `return None` 으로 뭉개기 |

| 예외 카테고리 | HTTP 상태 |
|---|---|
| `NotFoundError` | 404 |
| `ValidationError` | 400 / 422 |
| `ConflictError` | 409 |
| `UnauthorizedError` | 401 / 403 |
| `ExternalError` | 502 / 503 |

```python
# core/exceptions.py
class AppError(Exception):
    """모든 도메인 예외의 베이스."""

class NotFoundError(AppError): ...
class OrderNotFoundError(NotFoundError):
    def __init__(self, order_id: str) -> None:
        super().__init__(f"order not found: {order_id}")
        self.order_id = order_id

# main.py — 예외 → HTTP 매핑은 여기 한곳에서만
@app.exception_handler(NotFoundError)
async def handle_not_found(_: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"message": str(exc)})
```

---

## 6. 로깅

| 규칙 | 내용 |
|---|---|
| 로거 획득 | 모듈마다 `logger = logging.getLogger(__name__)`. `print` 금지 |
| **포맷 — 구조화형** | 메시지에 `[...]` prefix 를 붙이지 않는다. 대신 `extra` 로 **`service`·`action` 등을 필드로 분리**한다. 운영에서 JSON 로깅·검색·대시보드에 유리 |
| 메시지 | `message` 는 사람이 읽을 짧은 설명만. 식별자·맥락은 필드로 |
| 레벨 기준 | `DEBUG`(개발 상세) / `INFO`(주요 비즈니스 이벤트) / `WARNING`(복구 가능한 이상) / `ERROR`(처리 실패·예외) / `CRITICAL`(서비스 중단) |
| 예외 로깅 | `logger.exception(...)` 또는 `logger.error(..., exc_info=True)` 로 스택 포함 |
| 민감정보 | 비밀번호·토큰·API 키·개인정보는 필드에도 남기지 않거나 마스킹 |
| 상관관계 | 요청 ID(correlation id)를 필드로 포함해 추적 가능하게 (미들웨어 권장) |

**구조화형으로 기록한다.** `service`(서비스/도메인), `action`(동작/메서드), 그 밖의 맥락(식별자 등)을 `extra` 필드로 넘긴다.

```python
logger = logging.getLogger(__name__)

async def cancel_order(self, order_id: str) -> Order:
    logger.info(
        "cancel order requested",
        extra={"service": "OrderService", "action": "cancel_order", "order_id": order_id},
    )
    try:
        ...
    except PaymentError:
        logger.exception(  # 스택 자동 포함
            "refund failed",
            extra={"service": "OrderService", "action": "cancel_order", "order_id": order_id},
        )
        raise
```

> 필드 표준: `service` 는 서비스/도메인 이름(예: `OrderService`), `action` 은 동작(보통 메서드명, 예: `cancel_order`). 검색·필터는 메시지 파싱이 아니라 이 필드로 한다.
>
> JSON 포매터(예: `python-json-logger`)를 붙이면 `extra` 필드가 그대로 JSON 키로 출력돼 로그 수집기에서 바로 질의할 수 있다.

---

## 7. 설정과 시크릿

| 규칙 | 내용 |
|---|---|
| 설정 소스 | Pydantic `BaseSettings` 로 환경변수를 주입. 전역 `settings` 한 객체로 관리 |
| 접근 방식 | 설정은 주입(`Depends`)으로 받는다. 코드 곳곳에서 `os.getenv` 직접 호출 금지 |
| 시크릿 | DB 비밀번호·API 키 등은 코드·레포에 하드코딩 금지 → 환경변수/시크릿 매니저 |
| `.env` | 로컬 전용. `.gitignore` 에 포함하고, 키 목록은 `.env.example` 로 공유 |
| 노출 금지 | 시크릿을 로그·에러 응답·예외 메시지에 절대 넣지 않는다 |

```python
# core/config.py
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    database_url: str
    openai_api_key: str
    default_page_size: int = 20

@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
```

---

## 8. 날짜·시간

| 규칙 | 내용 |
|---|---|
| 타임존 | 항상 **timezone-aware UTC** 로 저장·연산한다. naive datetime 금지 |
| 현재 시각 | `datetime.now(UTC)` 사용. `datetime.utcnow()` 는 naive 라 **금지**(deprecated) |
| DB 컬럼 | `TIMESTAMP WITH TIME ZONE` 사용 |
| 변환 시점 | 로컬 타임존 변환은 **표시(프론트/응답) 시점에만**. 서버 내부는 UTC 유지 |

```python
from datetime import UTC, datetime

now = datetime.now(UTC)        # ✅ aware UTC
# now = datetime.utcnow()      # ❌ naive — 금지
```

---

## 9. 페이지네이션

목록 조회는 날것의 `list` 대신 공통 래퍼로 감싼다. 상황에 맞는 **세 가지 모델** 중 하나를 쓴다.

| 모델 | 응답 필드 | count 쿼리 | 적합한 곳 |
|---|---|---|---|
| `Page[T]` | `items`, `total`, `page`, `size` | **함** (전체 개수 필요) | 페이지 번호·총 건수 표시 |
| `Slice[T]` | `items`, `page`, `size`, `has_next` | **안 함** | "더보기"·무한 스크롤 |
| `CursorPage[T]` | `items`, `next_cursor` | **안 함** | 대용량·정렬 안정성이 중요한 목록 |

공통 규칙

- 기본/최대 페이지 크기를 상수로 제한한다: **`DEFAULT_PAGE_SIZE = 20`, `MAX_PAGE_SIZE = 100`**. 요청이 최대치를 넘으면 100 으로 클램프한다.
- 전체 건수가 화면에 필요 없으면 `COUNT(*)` 비용을 줄이려 `Slice`/`CursorPage` 를 우선 고려한다.

```python
class Page[T](BaseModel):          # Python 3.12+ 제네릭 문법. total 포함
    items: list[T]
    total: int
    page: int
    size: int

class Slice[T](BaseModel):         # total 없이 다음 존재 여부만
    items: list[T]
    page: int
    size: int
    has_next: bool

class CursorPage[T](BaseModel):    # 커서 기반. next_cursor 가 None 이면 끝
    items: list[T]
    next_cursor: str | None
```

**Slice 구현** — `size + 1` 개를 조회해 초과분이 있으면 `has_next = True`.

```python
async def search_slice(self, page: int, size: int) -> Slice[Order]:
    offset = page * size
    stmt = select(Order).offset(offset).limit(size + 1)   # 1개 더 조회
    rows = list((await self._session.scalars(stmt)).all())
    has_next = len(rows) > size
    return Slice(items=rows[:size], page=page, size=size, has_next=has_next)
```

**Cursor 구현** — 마지막 행의 정렬 키를 커서로 넘겨 다음 조회의 시작점으로 쓴다(여기선 `id` 기준).

```python
async def search_cursor(self, cursor: str | None, size: int) -> CursorPage[Order]:
    stmt = select(Order).order_by(Order.id).limit(size + 1)
    if cursor is not None:
        stmt = stmt.where(Order.id > cursor)
    rows = list((await self._session.scalars(stmt)).all())
    has_next = len(rows) > size
    items = rows[:size]
    next_cursor = items[-1].id if has_next else None
    return CursorPage(items=items, next_cursor=next_cursor)
```

---

## 10. 테스트

| 규칙 | 내용 |
|---|---|
| 위치 | `tests/` 아래에 앱 구조를 미러링. 파일은 `test_{모듈}.py` |
| 함수명 | `test_{대상}_{시나리오}_{기대}` (§1 의 테스트 함수 규칙) |
| 구조 | given-when-then(arrange-act-assert) 으로 단계 구분 |
| 격리 | 외부 의존(Client·Repository)은 mock/fake 로 대체. Service 단위 테스트는 DB 없이 |
| 도구 | `pytest` + `pytest-asyncio`(async 테스트). API 테스트는 `httpx.AsyncClient` |
| 통합 테스트 | `@pytest.mark.integration` 등으로 분리해 단위 테스트와 구분 |

```python
async def test_cancel_order_marks_archived() -> None:
    # given
    repo = FakeOrderRepository(orders=[order_fixture(status="paid")])
    service = OrderService(repo, FakePaymentService(), FakeNotifier())
    # when
    result = await service.cancel_order("O-1")
    # then
    assert result.status == OrderStatus.CANCELLED
```

---

## 11. 린터 · 포매터 · 타입체크

| 도구 | 역할 |
|---|---|
| **ruff** | 린트 + 포매팅 + import 정렬 (단일 도구로 통일) |
| **mypy** (또는 pyright) | 정적 타입 검사 (§4 타입 힌트와 연계) |

- 설정은 `pyproject.toml` 한곳에 모은다(줄 길이·규칙·제외 경로 등). 도구별 설정 파일 난립 금지.
- CI 에서 `ruff check`, `ruff format --check`, `mypy` 통과를 **머지 조건**으로 둔다.
- 포매팅은 도구에 위임한다 — 스타일을 리뷰에서 다투지 않는다.

---

## 12. 주석 규칙

- **원칙**: 네이밍으로 의도를 전달한다. 주석은 "왜(rationale)" 와 "외부 계약" 에만.
- **Docstring**(`"""..."""`)은 공개 Service 메서드·Client 인터페이스·비자명한 도메인 규칙에 단다. 자명한 게터엔 달지 않는다.
- 외부 계약(예: 외부 API 가 요구하는 필드명·제약)은 주석으로 근거를 남긴다.
- TODO 포맷: `# TODO(담당자, YYYY-MM-DD): 문제 + 해결 방향`.

```python
# TODO(담당자, 2026-06-30): 재시도 횟수를 설정에서 주입받도록 변경.
```

---

## 13. 신규 코드 작성 체크리스트

새 파일/클래스를 만들기 전 자가 질문:

1. **어느 레이어인가?** Router / Service / Client / Repository / Model / Schema — 접미사로 드러나는가?
2. **읽기인가 쓰기인가?** 함수가 `get/find/search`(읽기) 또는 `create/update/delete`(쓰기)로 시작하는가?
3. **필드명이 프로젝트 표준(`snake_case`)과 일치하는가?**
4. **경계를 섞지 않았는가?** Router 에 로직 없음 / Repository 가 Model 만 반환 / Model 이 Schema 를 모름.
5. **외부 호출은 Client 를 통하는가?** 비동기 클라이언트를 썼는가?
6. **타입 힌트가 모든 시그니처에 있는가?**

```text
Q. 이 코드는 무엇을 하는가?
 ├ HTTP 요청 처리      → routers/{domain}_router.py  (함수: 동사)
 ├ 유스케이스 조율      → services/{domain}_service.py  ({Domain}Service)
 │    └ 특정 책임 전담   → services/{목적}_service.py
 ├ 외부 API 호출       → clients/  ({Provider}Client, 비동기)
 ├ DB 접근            → repositories/{domain}_repository.py  (get/find/search)
 ├ 영속 데이터 정의     → models/{domain}.py  (SQLAlchemy, snake_case 컬럼)
 ├ 요청/응답 형태       → schemas/{domain}.py  ({Action}{Domain}Request/Response)
 └ 변환               → 대상 타입의 classmethod/메서드 (from_model / to_model)
```

---

## 부록 A. 레이어별 네이밍 한눈 요약


| 레이어          | 네이밍 패턴                                   | 예                                               |
| ------------ | ---------------------------------------- | ----------------------------------------------- |
| Router(파일)   | `{domain}_router.py` + `router` 객체       | `order_router.py`                               |
| 엔드포인트 함수     | 동사 `snake_case`                          | `create_order`, `search_orders`, `cancel_order` |
| 요청 Schema    | `{Action}{Domain}Request`                | `CreateOrderRequest`                            |
| 응답 Schema    | `{Domain}Response`                       | `OrderResponse`                                 |
| 필터 Schema    | `{Domain}Filter`                         | `OrderFilter`                                   |
| Service      | `{Domain}Service` / `{목적}Service`        | `OrderService`, `PaymentService`                |
| Client 인터페이스 | `{역할}Client`(Protocol)                   | `PaymentClient`, `EmailClient`                  |
| Client 구현    | `{Provider}Client`                       | `TossPaymentClient`, `SesEmailClient`           |
| Repository   | `{Domain}Repository`                     | `OrderRepository`                               |
| Repo 메서드     | `get_by_*` / `find_by_*` / `search_by_*` | `get_by_id`, `find_by_user_id`                  |
| Model        | `PascalCase` 단수 + `__tablename__` snake  | `Order` / `"order"`                             |
| Model 상태 전이  | 동사 메서드                                   | `mark_as_paid()`, `cancel()`                    |
| 변환(Model→응답) | `Response.from_model(m)`                 | `OrderResponse.from_model(...)`                 |
| Enum         | `PascalCase(StrEnum)` + 코드 문자열           | `OrderStatus.PAID = "paid"`                     |


> **한 줄 결론**: 레이어는 접미사로(`_router`/`Service`/`Client`/`Repository`/Model·Schema), 동작은 동사로(`get`/`find`/`search`/`create`/`delete`), 필드는 `snake_case` 표준으로 — 이름만 보고 위치·의도·계약을 알 수 있게 한다.
>
>

