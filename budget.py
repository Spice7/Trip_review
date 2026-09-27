from datetime import datetime, timezone
from pathlib import Path

from storage import StateError, read_json, write_json


class BudgetExceeded(RuntimeError):
    pass


class EntityBudgetManager:
    """Use inside output_lock. Reserve durably BEFORE every HTTP attempt; never refund."""

    def __init__(self, path: Path, hard_limit: int = 800):
        if type(hard_limit) is not int or not 1 <= hard_limit <= 1000:
            raise ValueError("HARD_ENTITY_BUDGET은 1~1000 정수여야 합니다.")
        self.path = Path(path)
        self.hard_limit = hard_limit
        state = read_json(self.path)
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
        return max(0, self.hard_limit - self.estimated_used)

    def reserve(self, cost: int):
        if type(cost) is not int or cost < 1:
            raise ValueError("Entity cost must be a positive integer")
        if self.estimated_used + cost > self.hard_limit:
            raise BudgetExceeded(f"예산 부족: 남은 {self.remaining}, 다음 요청 예약 {cost}")
        self.estimated_used += cost
        self.save()  # Failure here propagates: never send an unrecorded request.

    def save(self):
        write_json(self.path, {
            "hard_limit": self.hard_limit,
            "estimated_used": self.estimated_used,
            "remaining_local_budget": self.remaining,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        })

    def summary(self):
        return (
            "\n========================================\nAPI Entity Budget\n"
            f"Estimated entities used: {self.estimated_used}\n"
            f"Hard limit: {self.hard_limit}\nRemaining local budget: {self.remaining}\n"
            "IMPORTANT: This is a conservative local estimate.\n"
            "The Tripadvisor Dashboard is the final source for actual billing/entity usage.\n"
            "========================================"
        )
