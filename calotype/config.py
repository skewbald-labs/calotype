"""Settings, read from environment variables, and the plan table.

Nothing secret lives in this file. Every value can be overridden with an
environment variable; see README.md for the list.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_PLANS_FILE = PACKAGE_DIR / "plans.json"
DEFAULT_DB_PATH = PACKAGE_DIR / "data" / "calotype.db"
DEFAULT_BLOCKLIST_FILE = PACKAGE_DIR / "adblock_domains.txt"
STORAGE_BACKENDS = ("sqlite", "firestore")
SITE_DIR = PACKAGE_DIR / "site"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class Plan:
    name: str
    monthly_quota: int
    price_usd_cents: int
    title: str = ""


@dataclass(frozen=True)
class Settings:
    # "sqlite" (database_url) or "firestore" (Google Cloud Firestore, Native mode).
    storage_backend: str = "sqlite"
    database_url: str = f"sqlite:///{DEFAULT_DB_PATH}"
    # Firestore only. An empty project means the default credentials' project.
    firestore_project: str = ""
    firestore_collection_prefix: str = "calotype_"
    plans_file: Path = DEFAULT_PLANS_FILE
    blocklist_file: Path = DEFAULT_BLOCKLIST_FILE
    # How many screenshots render at once, and how long a request may wait for a slot.
    max_concurrency: int = 4
    queue_timeout_s: int = 10
    # Hard limit on one capture, from navigation to encoded image.
    capture_timeout_s: int = 30
    # Bytes a single page may pull from the network, across all its requests.
    max_page_bytes: int = 50 * 1024 * 1024
    log_level: str = "INFO"
    serve_site: bool = True
    # Self-serve free keys (POST /v1/keys). Limits are held in memory only.
    self_serve_keys: bool = True
    keys_per_ip_per_hour: int = 3
    keys_global_per_hour: int = 200
    # Proxies in front of the app whose X-Forwarded-For entry is trusted.
    # 0 = use the socket peer. Cloud Run: 1.
    trusted_proxy_hops: int = 0
    # The public origin, e.g. https://calotype.example. Stripe sends buyers back
    # here after checkout and from the portal. Empty: taken from the request.
    public_url: str = ""
    plans: dict[str, Plan] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        db_url = os.environ.get("CALOTYPE_DATABASE_URL", "").strip()
        if not db_url:
            db_path = os.environ.get("CALOTYPE_DB_PATH", "").strip() or str(DEFAULT_DB_PATH)
            db_url = f"sqlite:///{db_path}"
        backend = os.environ.get("CALOTYPE_STORAGE", "").strip().lower() or "sqlite"
        if backend not in STORAGE_BACKENDS:
            raise ValueError(f"CALOTYPE_STORAGE must be one of {', '.join(STORAGE_BACKENDS)}, not {backend!r}")
        plans_file = Path(os.environ.get("CALOTYPE_PLANS_FILE", "") or DEFAULT_PLANS_FILE)
        blocklist = Path(os.environ.get("CALOTYPE_BLOCKLIST_FILE", "") or DEFAULT_BLOCKLIST_FILE)
        return cls(
            storage_backend=backend,
            database_url=db_url,
            firestore_project=os.environ.get("CALOTYPE_FIRESTORE_PROJECT", "").strip(),
            firestore_collection_prefix=os.environ.get("CALOTYPE_FIRESTORE_COLLECTION_PREFIX", "calotype_").strip(),
            plans_file=plans_file,
            blocklist_file=blocklist,
            max_concurrency=_env_int("CALOTYPE_MAX_CONCURRENCY", 4),
            queue_timeout_s=_env_int("CALOTYPE_QUEUE_TIMEOUT_S", 10),
            capture_timeout_s=min(_env_int("CALOTYPE_CAPTURE_TIMEOUT_S", 30), 30),
            max_page_bytes=_env_int("CALOTYPE_MAX_PAGE_BYTES", 50 * 1024 * 1024),
            log_level=os.environ.get("CALOTYPE_LOG_LEVEL", "INFO").upper(),
            serve_site=os.environ.get("CALOTYPE_SERVE_SITE", "1") not in ("0", "false", "no"),
            self_serve_keys=os.environ.get("CALOTYPE_SELF_SERVE_KEYS", "1") not in ("0", "false", "no"),
            keys_per_ip_per_hour=_env_int("CALOTYPE_KEYS_PER_IP_PER_HOUR", 3),
            keys_global_per_hour=_env_int("CALOTYPE_KEYS_GLOBAL_PER_HOUR", 200),
            trusted_proxy_hops=_env_int("CALOTYPE_TRUSTED_PROXY_HOPS", 0),
            public_url=os.environ.get("CALOTYPE_PUBLIC_URL", "").strip().rstrip("/"),
            plans=load_plans(plans_file),
        )

    def with_plans(self) -> "Settings":
        """Return a copy with plans loaded from plans_file if none are set."""
        if self.plans:
            return self
        from dataclasses import replace

        return replace(self, plans=load_plans(self.plans_file))


def load_plans(path: Path) -> dict[str, Plan]:
    data = json.loads(Path(path).read_text())
    plans: dict[str, Plan] = {}
    for name, spec in data["plans"].items():
        quota = int(spec["monthly_quota"])
        if quota < 0:
            raise ValueError(f"plan {name!r} has a negative quota")
        plans[name] = Plan(
            name=name,
            monthly_quota=quota,
            price_usd_cents=int(spec.get("price_usd_cents", 0)),
            title=str(spec.get("title", name.title())),
        )
    if not plans:
        raise ValueError(f"no plans defined in {path}")
    return plans
