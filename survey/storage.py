import json
import secrets
from datetime import datetime, timezone

import requests
from cryptography.fernet import Fernet, InvalidToken

JOB_TTL = 3600
LOCK_TTL = 150
MAX_STORED_JOB = 8 * 1024 * 1024 - 65536


class StorageError(RuntimeError):
    pass


class JobBusy(RuntimeError):
    pass


class QuotaExceeded(RuntimeError):
    pass


class JobCapacityExceeded(RuntimeError):
    pass


class RedisStore:
    """Encrypted, short-lived job storage shared by all serverless instances."""

    def __init__(self, settings):
        self.url = settings.redis_url
        self.token = settings.redis_token
        self.cipher = Fernet(settings.encryption_key.encode())

    def command(self, *args):
        try:
            response = requests.post(
                self.url,
                headers={"Authorization": f"Bearer {self.token}"},
                json=list(args), timeout=(5, 15), allow_redirects=False,
            )
            if response.status_code != 200:
                raise StorageError("Storage is temporarily unavailable.")
            result = response.json()
            if "error" in result or "result" not in result:
                raise StorageError("Storage is temporarily unavailable.")
            return result["result"]
        except (requests.RequestException, ValueError) as exc:
            raise StorageError("Storage is temporarily unavailable.") from exc

    @staticmethod
    def key(owner, job_id):
        return f"survey:v1:job:{owner}:{job_id}"

    def load(self, owner, job_id):
        value = self.command("GET", self.key(owner, job_id))
        if value is None:
            return None
        try:
            return json.loads(self.cipher.decrypt(value.encode(), ttl=JOB_TTL))
        except (InvalidToken, ValueError, TypeError) as exc:
            raise StorageError("The stored job cannot be read.") from exc

    def create(self, owner, job_id, job):
        encrypted = self.encrypt_job(job)
        result = self.command("SET", self.key(owner, job_id), encrypted, "NX", "EX", JOB_TTL)
        if result != "OK":
            raise StorageError("Could not create this job.")

    def save_locked(self, owner, job_id, job, lock_token):
        # A request that outlives its lock must never overwrite a newer result.
        script = """
        if redis.call('GET', KEYS[2]) ~= ARGV[1] then return 0 end
        if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
        redis.call('SET', KEYS[1], ARGV[2], 'KEEPTTL')
        return 1
        """
        encrypted = self.encrypt_job(job)
        key = self.key(owner, job_id)
        if self.command("EVAL", script, 2, key, key + ":lock", lock_token, encrypted) != 1:
            raise JobBusy("Job expired or another request acquired its lock.")

    def encrypt_job(self, job):
        payload = json.dumps(job, ensure_ascii=False, separators=(",", ":")).encode()
        encrypted = self.cipher.encrypt(payload).decode()
        if len(encrypted) > MAX_STORED_JOB:
            raise JobCapacityExceeded(
                "This job has exceeded its encrypted storage limit. Download completed languages, "
                "then split the survey into smaller workbooks or choose fewer languages."
            )
        return encrypted

    def lock(self, owner, job_id):
        token = secrets.token_urlsafe(32)
        key = self.key(owner, job_id) + ":lock"
        if self.command("SET", key, token, "NX", "EX", LOCK_TTL) != "OK":
            raise JobBusy("A batch is already running. Wait before resuming.")
        return token

    def unlock(self, owner, job_id, token):
        script = """
        if redis.call('GET', KEYS[1]) == ARGV[1] then
            return redis.call('DEL', KEYS[1])
        end
        return 0
        """
        self.command("EVAL", script, 1, self.key(owner, job_id) + ":lock", token)

    def delete(self, owner, job_id):
        self.command("DEL", self.key(owner, job_id))

    def quota(self, owner, category, user_limit, global_limit):
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        prefix = f"survey:v1:quota:{day}:{category}"
        script = """
        local user = tonumber(redis.call('GET', KEYS[1]) or '0')
        local total = tonumber(redis.call('GET', KEYS[2]) or '0')
        if user >= tonumber(ARGV[1]) or total >= tonumber(ARGV[2]) then return 0 end
        redis.call('INCR', KEYS[1])
        redis.call('INCR', KEYS[2])
        redis.call('EXPIRE', KEYS[1], 172800)
        redis.call('EXPIRE', KEYS[2], 172800)
        return 1
        """
        result = self.command(
            "EVAL", script, 2, f"{prefix}:{owner}", f"{prefix}:global",
            user_limit, global_limit,
        )
        if result != 1:
            raise QuotaExceeded("Daily usage limit reached. Try again tomorrow (UTC).")
