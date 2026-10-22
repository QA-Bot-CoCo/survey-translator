import os
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    api_key: str = ""
    model: str = "gpt-6-luna"
    origin: str = ""

    @classmethod
    def from_env(cls):
        origin = os.environ.get("APP_ORIGIN", "").strip().rstrip("/")
        parsed = urlsplit(origin)
        if origin and (
            parsed.scheme not in ("https", "http") or not parsed.hostname
            or parsed.username or parsed.password or parsed.path
            or parsed.query or parsed.fragment
            or (parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1"))
        ):
            raise ValueError("APP_ORIGIN must be an HTTPS origin (HTTP is allowed for localhost).")
        return cls(
            api_key=os.environ.get("OPENAI_API_KEY", "").strip(),
            model=os.environ.get("OPENAI_MODEL", "gpt-6-luna").strip(),
            origin=origin,
        )
