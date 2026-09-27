from datetime import datetime, timezone
from pathlib import Path
import os

from storage import StateError, read_json, write_json


class BudgetExceeded(RuntimeError):
    pass


class EntityBudgetManager:
    """Use inside output_lock. Reserve durably BEFORE every HTTP attempt; never refund."""

    def __init__(self, path: Path, hard_limit: int = 800):
        if type(hard_limit) is not int or hard_limit < 1:
            raise ValueError("HARD_ENTITY_BUDGET은 양의 정수여야 합니다.")
        self.path = Path(path)
        self.hard_limit = hard_limit
        self.run_ceiling = None
        state = read_json(self.path)
        self.metadata = dict(state) if isinstance(state, dict) else {}
        # If caches survive but the ledger disappears, resetting to zero is unsafe.
        if state is None and not self.path.exists():
            root = self.path.parent
            if (root / "reviews.json").exists() or any((root / "cache").rglob("*.json")):
                raise StateError("캐시/결과가 있지만 사용량 원장이 없습니다. 원장을 복구하세요.")
            self.estimated_used = 0
        elif (not isinstance(state, dict)
              or type(state.get("estimated_used")) is not int
              or state["estimated_used"] < 0):
            raise StateError("entity_usage.json 누적 사용량이 유효하지 않습니다.")
        else:
            self.estimated_used = state["estimated_used"]

    @property
    def remaining(self):
        ceiling = min(self.hard_limit, self.run_ceiling) if self.run_ceiling is not None else self.hard_limit
        return max(0, ceiling - self.estimated_used)

    def limit_run(self, entities):
        if type(entities) is not int or entities < 1:
            raise ValueError("실행별 예산은 양의 정수여야 합니다.")
        self.run_ceiling = self.estimated_used + entities

    def reserve(self, cost: int):
        if type(cost) is not int or cost < 1:
            raise ValueError("Entity cost must be a positive integer")
        if cost > self.remaining:
            raise BudgetExceeded(f"예산 부족: 남은 {self.remaining}, 다음 요청 예약 {cost}")
        self.estimated_used += cost
        try:
            self.save()  # Failure here propagates: never send an unrecorded request.
        except OSError:
            self.estimated_used -= cost  # No HTTP attempt was allowed to start.
            raise

    def save(self):
        write_json(self.path, {
            **self.metadata,
            "billing_estimate_model": "one_per_http_attempt_account_observation",
            "hard_limit": self.hard_limit,
            "estimated_used": self.estimated_used,
            "remaining_local_budget": max(0, self.hard_limit - self.estimated_used),
            "last_updated": datetime.now(timezone.utc).isoformat(),
        })

    def sync_dashboard_usage(self, usage):
        """Call only after explicit CLI confirmation while holding output_lock."""
        if type(usage) is not int or usage < 0 or usage > self.hard_limit:
            raise ValueError("Dashboard usage는 0~HARD_ENTITY_BUDGET 범위여야 합니다.")
        now = datetime.now(timezone.utc)
        backup = self.path.with_name(f"entity_usage.backup_{now.strftime('%Y%m%dT%H%M%S%fZ')}.json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            with backup.open("xb") as stream:
                stream.write(self.path.read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
        else:
            write_json(backup, {"estimated_used": self.estimated_used, "hard_limit": self.hard_limit})
        previous = self.estimated_used
        previous_meta = self.metadata
        event = {"dashboard_synced_usage": usage, "dashboard_synced_at": now.isoformat(),
                 "previous_estimated_used": previous, "backup_file": backup.name}
        history = self.metadata.get("dashboard_sync_history", [])
        if not isinstance(history, list):
            raise StateError("동기화 이력 형식이 올바르지 않습니다.")
        self.metadata = {**self.metadata, **event, "dashboard_sync_history": [*history, event]}
        self.estimated_used = usage
        try:
            self.save()
        except OSError:
            self.estimated_used, self.metadata = previous, previous_meta
            raise
        return backup

    def summary(self):
        run_info = (f"Remaining this run: {self.remaining}\n"
                    f"Run ceiling (cumulative): {self.run_ceiling}\n"
                    if self.run_ceiling is not None else "")
        return (
            "\n========================================\nAPI Entity Budget\n"
            f"Estimated entities used: {self.estimated_used}\n"
            f"Hard limit: {self.hard_limit}\n"
            f"Remaining local budget: {max(0, self.hard_limit - self.estimated_used)}\n"
            f"{run_info}"
            "IMPORTANT: Account-specific estimate: 1 per HTTP attempt; failures also count locally.\n"
            "The Tripadvisor Dashboard is the final source for actual billing/entity usage.\n"
            "========================================"
        )
