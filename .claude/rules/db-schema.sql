-- 물리 DB 스키마(DDL) 단일 출처 — PostgreSQL.
-- app/models/*.py, alembic/versions/ 와 일치시킨다 (.claude/rules/db-migration.md).
-- Enum 컬럼은 VARCHAR(32) 에 app/enums.py 의 값(value)을 저장한다. CHECK 제약은 두지 않는다.
-- revision: cf3b3859224c (create build tables)

CREATE TABLE users (
    id SERIAL NOT NULL,
    github_user_id BIGINT NOT NULL,
    login VARCHAR NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_users PRIMARY KEY (id),
    CONSTRAINT uq_users_github_user_id UNIQUE (github_user_id)
);

CREATE TABLE services (
    id SERIAL NOT NULL,
    owner_user_id INTEGER NOT NULL,
    name VARCHAR NOT NULL,
    slug VARCHAR NOT NULL,
    github_repository_id BIGINT NOT NULL,
    repository_full_name VARCHAR NOT NULL,
    github_installation_id BIGINT,
    root_directory VARCHAR DEFAULT '.' NOT NULL,
    builder VARCHAR(32) DEFAULT 'auto' NOT NULL,
    dockerfile_path VARCHAR,
    auto_deploy BOOLEAN DEFAULT 'false' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_services PRIMARY KEY (id),
    CONSTRAINT fk_services_owner_user_id_users FOREIGN KEY(owner_user_id) REFERENCES users (id),
    CONSTRAINT uq_services_slug UNIQUE (slug)
);

CREATE INDEX ix_services_github_repository_id ON services (github_repository_id);

CREATE INDEX ix_services_owner_user_id ON services (owner_user_id);

CREATE TABLE deployment_requests (
    id SERIAL NOT NULL,
    service_id INTEGER NOT NULL,
    environment VARCHAR(32) DEFAULT 'prod' NOT NULL,
    trigger VARCHAR(32) NOT NULL,
    source_sha VARCHAR,
    idempotency_key VARCHAR NOT NULL,
    requested_by INTEGER,
    status VARCHAR(32) DEFAULT 'QUEUED' NOT NULL,
    failure_code VARCHAR(32),
    cancel_requested_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_deployment_requests PRIMARY KEY (id),
    CONSTRAINT fk_deployment_requests_requested_by_users FOREIGN KEY(requested_by) REFERENCES users (id),
    CONSTRAINT fk_deployment_requests_service_id_services FOREIGN KEY(service_id) REFERENCES services (id),
    CONSTRAINT uq_deployment_requests_idempotency_key UNIQUE (idempotency_key)
);

CREATE INDEX ix_deployment_requests_service_id ON deployment_requests (service_id);

CREATE TABLE builds (
    id SERIAL NOT NULL,
    deployment_request_id INTEGER NOT NULL,
    status VARCHAR(32) DEFAULT 'PENDING' NOT NULL,
    builder VARCHAR(32),
    source_sha VARCHAR,
    codebuild_build_id VARCHAR,
    attempt INTEGER DEFAULT '1' NOT NULL,
    image_repository VARCHAR,
    image_tag VARCHAR,
    image_digest VARCHAR,
    deploy_config JSONB,
    failure_code VARCHAR(32),
    log_url VARCHAR,
    started_at TIMESTAMP WITH TIME ZONE,
    finished_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_builds PRIMARY KEY (id),
    CONSTRAINT fk_builds_deployment_request_id_deployment_requests FOREIGN KEY(deployment_request_id) REFERENCES deployment_requests (id),
    CONSTRAINT uq_builds_deployment_request_id UNIQUE (deployment_request_id)
);

CREATE TABLE jobs (
    id SERIAL NOT NULL,
    deployment_request_id INTEGER NOT NULL,
    kind VARCHAR(32) NOT NULL,
    status VARCHAR(32) DEFAULT 'QUEUED' NOT NULL,
    payload JSONB NOT NULL,
    priority INTEGER DEFAULT '0' NOT NULL,
    run_after TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    attempts INTEGER DEFAULT '0' NOT NULL,
    max_attempts INTEGER DEFAULT '3' NOT NULL,
    locked_by VARCHAR,
    locked_until TIMESTAMP WITH TIME ZONE,
    external_id VARCHAR,
    last_error VARCHAR,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_jobs PRIMARY KEY (id),
    CONSTRAINT fk_jobs_deployment_request_id_deployment_requests FOREIGN KEY(deployment_request_id) REFERENCES deployment_requests (id)
);

CREATE INDEX ix_jobs_deployment_request_id ON jobs (deployment_request_id);

CREATE INDEX ix_jobs_status_run_after ON jobs (status, run_after);

