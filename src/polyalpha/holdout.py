"""Final holdout lock — prevents repeated evaluation of the test set.

Section 69 of the v0.3 spec: maintain a genuinely untouched holdout period
and require explicit --unlock-final-holdout to evaluate it. Creates friction
against accidentally turning the "test set" into another validation set.
"""

import hashlib
import json
from datetime import datetime
from pathlib import Path

HOLDOUT_MARKER = ".holdout_unlocked"


def _holdout_state_path(config_dir: str = "data") -> Path:
    return Path(config_dir) / "holdout_state.json"


def lock_holdout(config_dir: str = "data") -> dict:
    """Lock the final holdout period."""
    state = {
        "locked": True,
        "locked_at": datetime.utcnow().isoformat(),
        "evaluation_count": 0,
        "last_evaluation": None,
    }
    path = _holdout_state_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f, indent=2)
    return state


def check_holdout_lock(config_dir: str = "data") -> dict:
    """Check if holdout is locked. Returns state dict."""
    path = _holdout_state_path(config_dir)
    if not path.exists():
        return lock_holdout(config_dir)
    with open(path) as f:
        return json.load(f)


def unlock_holdout(config_dir: str = "data", reason: str = "") -> dict:
    """Explicitly unlock the holdout. Logs the event."""
    state = check_holdout_lock(config_dir)
    state["locked"] = False
    state["unlocked_at"] = datetime.utcnow().isoformat()
    state["unlock_reason"] = reason
    path = _holdout_state_path(config_dir)
    with open(path, "w") as f:
        json.dump(state, f, indent=2)
    return state


def record_holdout_evaluation(config_dir: str = "data") -> dict:
    """Record that the holdout was evaluated. Warns if repeatedly evaluated."""
    state = check_holdout_lock(config_dir)
    if state.get("locked", True):
        raise RuntimeError(
            "HOLDOUT LOCKED: Cannot evaluate final holdout. "
            "Use --unlock-final-holdout with a justification."
        )
    state["evaluation_count"] = state.get("evaluation_count", 0) + 1
    state["last_evaluation"] = datetime.utcnow().isoformat()
    if state["evaluation_count"] > 3:
        state["warning"] = (
            f"HOLDOUT EVALUATED {state['evaluation_count']} TIMES. "
            "This is no longer a pristine holdout. "
            "Results may be contaminated by repeated selection."
        )
    path = _holdout_state_path(config_dir)
    with open(path, "w") as f:
        json.dump(state, f, indent=2)
    return state


def get_holdout_hash(config_dir: str = "data") -> str:
    """Get a fingerprint of the holdout state for experiment tracking."""
    state = check_holdout_lock(config_dir)
    content = json.dumps(state, sort_keys=True)
    return hashlib.sha256(content.encode()).hexdigest()[:12]
