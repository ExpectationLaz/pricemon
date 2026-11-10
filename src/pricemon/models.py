"""Plain data objects passed between the stages of a run."""

from dataclasses import dataclass, field
from decimal import Decimal

# Observation and fetch statuses
OK = "ok"
FETCH_ERROR = "fetch_error"
BLOCKED = "blocked"
DISALLOWED = "disallowed"
NOT_FOUND = "not_found"
EXTRACT_ERROR = "extract_error"
VERIFY = "verify"  # extracted fine, but the price jump is held until the operator checks it


@dataclass(frozen=True)
class FetchResult:
    status: str
    http_status: int | None = None
    html: str | None = None
    error: str | None = None


@dataclass
class Partial:
    """What one extraction step found. Any field may be missing."""

    title: str | None = None
    price: Decimal | None = None
    currency: str | None = None
    in_stock: bool | None = None
    note: str | None = None  # why a step that matched still gave no price


@dataclass(frozen=True)
class Extraction:
    title: str | None
    price: Decimal
    currency: str | None
    in_stock: bool | None
    method: str  # the step that produced the price: jsonld, meta or rules
    sources: dict[str, str] = field(default_factory=dict)  # field name -> step


@dataclass(frozen=True)
class Observation:
    url: str
    product_name: str
    fetched_at: str
    status: str
    http_status: int | None = None
    title: str | None = None
    price: Decimal | None = None
    currency: str | None = None
    in_stock: bool | None = None
    method: str | None = None
    error: str | None = None
    id: int | None = None


@dataclass(frozen=True)
class Event:
    url: str
    product_name: str
    type: str  # price_down, price_up, out_of_stock, back_in_stock
    old_price: Decimal | None
    new_price: Decimal | None
    change_pct: Decimal | None
    verify: bool
    alerted: bool
    currency: str | None = None
    # Set when the event is read back from the store. Left out of comparisons so
    # an event built by hand equals the same event read back.
    id: int | None = field(default=None, compare=False)
    run_id: int | None = field(default=None, compare=False)


@dataclass
class RunSummary:
    client: str
    run_id: int
    dry_run: bool = False
    urls_total: int = 0
    urls_ok: int = 0
    urls_failed: int = 0
    events: list[Event] = field(default_factory=list)
    report_paths: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    undelivered: int = 0  # alerted events still waiting to reach the client
