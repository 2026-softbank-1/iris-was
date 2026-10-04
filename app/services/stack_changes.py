"""스택 재분석 결과와 기준(마지막으로 apply 한 분석)의 차이. 스택 `pendingChanges.changes` 가 된다.

서비스가 아니라 기준 분석과 비교한다. apply 에서 일부러 고르지 않은 unit 이 푸시마다 "추가"로 보이지
않게 하기 위해서다. 사라진 unit 도 서비스를 지우지 않고 UNIT_REMOVED 로만 알린다.
"""

from collections.abc import Mapping
from typing import Any

# 바뀌면 알리는 unit 필드(analysis-gate.v1 표기).
_UNIT_FIELDS = ("port", "rootDirectory", "builder", "dockerfilePath")


def _by_id(items: object) -> dict[str, Mapping[str, Any]]:
    if not isinstance(items, list):
        return {}
    return {
        str(item["id"]): item
        for item in items
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }


def compute_stack_changes(
    baseline: Mapping[str, Any] | None, current: Mapping[str, Any]
) -> list[dict[str, Any]]:
    base_units = _by_id((baseline or {}).get("units"))
    new_units = _by_id(current.get("units"))
    base_deps = _by_id((baseline or {}).get("dependencies"))
    new_deps = _by_id(current.get("dependencies"))
    changes: list[dict[str, Any]] = []
    for unit_id in sorted(new_units.keys() - base_units.keys()):
        changes.append({"type": "UNIT_ADDED", "unitId": unit_id})
    for unit_id in sorted(base_units.keys() - new_units.keys()):
        changes.append({"type": "UNIT_REMOVED", "unitId": unit_id})
    for unit_id in sorted(base_units.keys() & new_units.keys()):
        for field in _UNIT_FIELDS:
            before, after = base_units[unit_id].get(field), new_units[unit_id].get(field)
            if before != after:
                changes.append(
                    {
                        "type": "UNIT_CHANGED",
                        "unitId": unit_id,
                        "field": field,
                        "from": before,
                        "to": after,
                    }
                )
    for dependency_id in sorted(new_deps.keys() - base_deps.keys()):
        changes.append({"type": "DEPENDENCY_ADDED", "unitId": dependency_id})
    for dependency_id in sorted(base_deps.keys() - new_deps.keys()):
        changes.append({"type": "DEPENDENCY_REMOVED", "unitId": dependency_id})
    return changes
