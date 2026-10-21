import os
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import UUID


@dataclass(frozen=True)
class Settings:
    origin: str
    session_secret: str
    encryption_key: str
    tenant_id: str
    client_id: str
    client_secret: str
    allowed_users: frozenset[str]
    redis_url: str
    redis_token: str
    api_key: str
    model: str
    user_jobs: int = 10
    global_jobs: int = 100
    user_batches: int = 200
    global_batches: int = 1000

    @classmethod
    def from_env(cls):
        def required(name):
            value = os.environ.get(name, "").strip()
            if not value:
                raise ValueError(f"Missing configuration: {name}")
            return value

        def positive(name, default):
            value = int(os.environ.get(name, default))
            if not 1 <= value <= 10000:
                raise ValueError(f"Invalid limit: {name}")
            return value

        origin = required("APP_ORIGIN").rstrip("/")
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "https" or not parsed.hostname
            or parsed.username or parsed.password or parsed.path
            or parsed.query or parsed.fragment
        ):
            raise ValueError("APP_ORIGIN must be a bare HTTPS origin.")
        secret = required("SESSION_SECRET")
        if len(secret) < 64:
            raise ValueError("SESSION_SECRET must contain at least 64 characters.")
        redis_url = required("UPSTASH_REDIS_REST_URL").rstrip("/")
        redis = urlsplit(redis_url)
        if (
            redis.scheme != "https" or not redis.hostname
            or not redis.hostname.endswith(".upstash.io")
            or redis.username or redis.password or redis.path
            or redis.query or redis.fragment or redis.port not in (None, 443)
        ):
            raise ValueError("Use a dedicated HTTPS Upstash Redis REST endpoint.")
        return cls(
            origin=origin,
            session_secret=secret,
            encryption_key=required("DATA_ENCRYPTION_KEY"),
            tenant_id=str(UUID(required("ENTRA_TENANT_ID"))),
            client_id=str(UUID(required("ENTRA_CLIENT_ID"))),
            client_secret=required("ENTRA_CLIENT_SECRET"),
            allowed_users=frozenset(
                str(UUID(value.strip()))
                for value in required("ALLOWED_USER_IDS").split(",")
            ),
            redis_url=redis_url,
            redis_token=required("UPSTASH_REDIS_REST_TOKEN"),
            api_key=required("OPENAI_API_KEY"),
            model=os.environ.get("OPENAI_MODEL", "gpt-6-luna"),
            user_jobs=positive("USER_DAILY_JOBS", 10),
            global_jobs=positive("GLOBAL_DAILY_JOBS", 100),
            user_batches=positive("USER_DAILY_BATCHES", 200),
            global_batches=positive("GLOBAL_DAILY_BATCHES", 1000),
        )
