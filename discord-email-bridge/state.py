"""Simple local JSON state file for tracking processed emails.

Used to avoid processing (and re-sending to Discord) the same email twice.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Set

logger = logging.getLogger(__name__)


def _empty_state() -> dict:
    return {"processed_email_ids": [], "last_updated": ""}


class State:
    """In-memory view of state.json, with helpers to persist changes."""

    def __init__(self, state_file: str):
        self.state_file = Path(state_file)
        self.processed_email_ids: Set[str] = set()
        self._load()

    def _load(self) -> None:
        if not self.state_file.exists():
            logger.info("State file %s not found, creating a new one.", self.state_file)
            self._save_raw(_empty_state())
            self.processed_email_ids = set()
            return

        try:
            raw_text = self.state_file.read_text(encoding="utf-8")
            data = json.loads(raw_text)
            ids = data.get("processed_email_ids", [])
            if not isinstance(ids, list):
                raise ValueError("processed_email_ids is not a list")
            self.processed_email_ids = set(ids)
            logger.info("Loaded state file with %d processed email id(s).", len(self.processed_email_ids))
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            logger.error("Failed to read state file %s (%s). Creating a fresh state file.", self.state_file, exc)
            self.processed_email_ids = set()
            self._save_raw(_empty_state())

    def is_processed(self, email_id: str) -> bool:
        return email_id in self.processed_email_ids

    def mark_processed(self, email_id: str) -> None:
        self.processed_email_ids.add(email_id)
        self._save_raw(
            {
                "processed_email_ids": sorted(self.processed_email_ids),
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
        )

    def _save_raw(self, data: dict) -> None:
        try:
            self.state_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            logger.error("Failed to save state file %s: %s", self.state_file, exc)
