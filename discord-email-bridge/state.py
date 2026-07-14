"""Local JSON state: processed emails, and the Discord <-> Email message mapping.

See docs/discord-email-message-mapping.md for the design this implements.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Set

logger = logging.getLogger(__name__)

STATE_VERSION = 1


def normalize_content(text: Optional[str]) -> str:
    """Normalize message text for edit comparison (see discord-message-edit-delete-sync.md #6)."""
    if not text:
        return ""
    return text.replace("\r\n", "\n").strip()


def normalize_message_id(value: Optional[str]) -> Optional[str]:
    """Normalize an email Message-ID to the canonical '<...>' form.

    Returns None for empty/missing input. Must be applied before any
    Message-ID is stored, compared, or looked up.
    """
    if not value:
        return None

    value = value.strip()
    if not value:
        return None

    if not value.startswith("<"):
        value = "<" + value
    if not value.endswith(">"):
        value = value + ">"

    return value


def _empty_state() -> dict:
    return {
        "version": STATE_VERSION,
        "processed_email_ids": [],
        "message_mappings": {},
        "email_message_index": {},
        "last_updated": "",
    }


class State:
    """In-memory view of state.json, with helpers to persist changes.

    - processed_email_ids: dedup set for inbound emails already delivered to Discord.
    - message_mappings: discord_message_id -> mapping dict (see docs for schema).
    - email_message_index: normalized email Message-ID -> discord_message_id.
    """

    def __init__(self, state_file: str):
        self.state_file = Path(state_file)
        self.processed_email_ids: Set[str] = set()
        self.message_mappings: dict = {}
        self.email_message_index: dict = {}
        self._load()

    def _load(self) -> None:
        if not self.state_file.exists():
            logger.info("State file %s not found, creating a new one.", self.state_file)
            self._reset()
            self._save()
            return

        try:
            raw_text = self.state_file.read_text(encoding="utf-8")
            data = json.loads(raw_text)
            self._load_from_dict(data)
            logger.info(
                "Loaded state file with %d processed email id(s) and %d message mapping(s).",
                len(self.processed_email_ids),
                len(self.message_mappings),
            )
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            logger.error(
                "Failed to read state file %s (%s). Backing up and creating a fresh state file.",
                self.state_file,
                exc,
            )
            self._backup_corrupt_file()
            self._reset()
            self._save()

    def _load_from_dict(self, data: dict) -> None:
        if not isinstance(data, dict):
            raise ValueError("state root is not an object")

        ids = data.get("processed_email_ids", [])
        if not isinstance(ids, list):
            raise ValueError("processed_email_ids is not a list")

        mappings = data.get("message_mappings", {})
        if not isinstance(mappings, dict):
            raise ValueError("message_mappings is not an object")

        index = data.get("email_message_index", {})
        if not isinstance(index, dict):
            raise ValueError("email_message_index is not an object")

        self.processed_email_ids = set(ids)
        self.message_mappings = mappings
        self.email_message_index = index

    def _backup_corrupt_file(self) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = self.state_file.with_suffix(f"{self.state_file.suffix}.corrupt-{timestamp}")
        try:
            self.state_file.replace(backup_path)
            logger.error("Backed up corrupt state file to %s", backup_path)
        except OSError:
            logger.exception("Failed to back up corrupt state file %s", self.state_file)

    def _reset(self) -> None:
        self.processed_email_ids = set()
        self.message_mappings = {}
        self.email_message_index = {}

    # --- Discord message mapping -----------------------------------------

    def has_discord_message(self, discord_message_id: str) -> bool:
        return discord_message_id in self.message_mappings

    def get_by_discord_message_id(self, discord_message_id: str) -> Optional[dict]:
        return self.message_mappings.get(discord_message_id)

    def get_by_email_message_id(self, email_message_id: Optional[str]) -> Optional[dict]:
        normalized = normalize_message_id(email_message_id)
        if not normalized:
            return None
        discord_message_id = self.email_message_index.get(normalized)
        if not discord_message_id:
            return None
        return self.message_mappings.get(discord_message_id)

    def get_by_bridge_id(self, bridge_id: Optional[str]) -> Optional[dict]:
        if not bridge_id:
            return None
        for mapping in self.message_mappings.values():
            if mapping.get("bridge_id") == bridge_id:
                return mapping
        return None

    def add_mapping(self, mapping: dict) -> None:
        # Edit/delete lifecycle fields (see discord-message-edit-delete-sync.md #4).
        mapping.setdefault("status", "active")
        mapping.setdefault("edit_version", 0)
        mapping.setdefault("last_edit_fingerprint", None)
        mapping.setdefault("edited_at", None)
        mapping.setdefault("deleted_at", None)
        mapping.setdefault("delete_notification_sent", False)

        discord_message_id = mapping["discord_message_id"]
        self.message_mappings[discord_message_id] = mapping

        email_message_id = normalize_message_id(mapping.get("email_message_id"))
        if email_message_id:
            self.email_message_index[email_message_id] = discord_message_id

        self._save()

    def record_edit(
        self,
        discord_message_id: str,
        new_content: str,
        fingerprint: str,
        edited_at: str,
    ) -> None:
        """Persist a successfully-notified edit. Only call after the notification email is sent."""
        mapping = self.message_mappings.get(discord_message_id)
        if mapping is None:
            return
        mapping["content"] = new_content
        mapping["edit_version"] = mapping.get("edit_version", 0) + 1
        mapping["last_edit_fingerprint"] = fingerprint
        mapping["edited_at"] = edited_at
        self._save()

    def record_delete(self, discord_message_id: str, deleted_at: str) -> None:
        """Persist a successfully-notified delete. Only call after the notification email is sent."""
        mapping = self.message_mappings.get(discord_message_id)
        if mapping is None:
            return
        mapping["status"] = "deleted"
        mapping["deleted_at"] = deleted_at
        mapping["delete_notification_sent"] = True
        self._save()

    def is_deleted(self, discord_message_id: str) -> bool:
        mapping = self.message_mappings.get(discord_message_id)
        return bool(mapping and mapping.get("status") == "deleted")

    # --- Email dedup -------------------------------------------------------

    def is_email_processed(self, email_id: str) -> bool:
        return email_id in self.processed_email_ids

    def mark_email_processed(self, email_id: str) -> None:
        self.processed_email_ids.add(email_id)
        self._save()

    # --- Persistence ---------------------------------------------------------

    def save(self) -> None:
        self._save()

    def _save(self) -> None:
        data = {
            "version": STATE_VERSION,
            "processed_email_ids": sorted(self.processed_email_ids),
            "message_mappings": self.message_mappings,
            "email_message_index": self.email_message_index,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        self._save_atomic(data)

    def _save_atomic(self, data: dict) -> None:
        tmp_path = self.state_file.with_suffix(f"{self.state_file.suffix}.tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(json.dumps(data, indent=2, ensure_ascii=False))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.state_file)
        except OSError as exc:
            logger.error("Failed to save state file %s: %s", self.state_file, exc)
