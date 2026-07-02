import copy
import hashlib
import json
import os
import re
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from world_state_layers import (
    build_runtime_agent_state,
    build_runtime_event_db,
    build_runtime_log,
    build_runtime_relationship_db,
    build_simulation_state_db,
    load_layer_sidecars,
)


STEP17_SCHEMA_VERSION = "1.0"
ALLOWED_VALIDATION_STATUSES = {
    "allowed",
    "blocked",
    "uncertain",
    "needs_resolution",
}
STATEFUL_IMPACT_LEVELS = {"state_change", "high_impact"}
NON_STATEFUL_IMPACT_LEVELS = {"dialogue", "minor_action"}
GENERIC_EVENT_FIELDS = {
    "status",
    "location_id",
    "holder_id",
    "owner_id",
    "condition",
    "availability",
    "relationship",
    "knowledge",
    "presence",
    "current_owner_ids",
    "current_user_ids",
    "current_holder_ids",
    "resource_status",
    "acquired_by",
    "released_by",
}

RUNTIME_CHARACTER_DEFAULTS = {
    "health": {
        "current": 100,
        "maximum": 100,
        "status": "状态良好",
    },
    "current_location": None,
    "posture": "",
    "current_activity": "",
    "held_items": [],
    "clothing": "",
    "mood": "",
    "attention_target": "",
    "short_term_goal": "",
    "long_term_goal": "",
    "recent_memories": [],
    "known_information": [],
    "physical_state": "",
    "availability": "available",
    "equipment": [],
    "visible_injuries": [],
    "active_effects": [],
    "physiology": {
        "species": "",
        "sex": "",
        "apparent_age": "",
        "height": "",
        "build": "",
        "other": [],
    },
}

RUNTIME_LOCATION_DEFAULTS = {
    "time_of_day": "",
    "weather": "",
    "lighting": "",
    "ambient_sound": "",
    "present_characters": [],
    "visible_objects": [],
    "ongoing_events": [],
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def stable_hash(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def deep_copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


def atomic_write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def extract_json_object(text):
    decoder = json.JSONDecoder()
    text = str(text or "")
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("LLM response did not contain a JSON object.")


def compact_list(values, limit):
    result = []
    seen = set()
    for value in values:
        marker = stable_hash(value) if isinstance(value, (dict, list)) else str(value)
        if not marker or marker in seen:
            continue
        seen.add(marker)
        result.append(value)
        if len(result) >= limit:
            break
    return result


def bounded_int(value, default=0, minimum=0, maximum=1440):
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    return max(minimum, min(maximum, result))


class SimulationStore:
    """Event-sourced mutable state kept separate from the read-only world DB."""

    def __init__(
        self,
        world_db,
        character_db,
        agent_profiles,
        path=Path("simulation_state.json"),
    ):
        self.world_db = world_db
        self.character_db = character_db
        self.agent_profiles = agent_profiles
        self.path = Path(path)
        self.world_fingerprint = world_db.get("world_db_fingerprint") or stable_hash(
            world_db
        )
        self.agent_fingerprint = agent_profiles.get(
            "agent_profile_db_fingerprint"
        ) or stable_hash(agent_profiles)
        self.runtime_dir = self.path.parent
        self.agents_dir = self.runtime_dir.parent / "agents"
        self.state = self._load_or_create()
        self._refresh_runtime_sidecars()
        self._sync_sidecar_files()

    def _simulation_template(self):
        return self.world_db.get("simulation_state_template") or self.world_db.get(
            "simulation_state_db", {}
        )

    def _base_entity_states(self):
        layered_state = self._simulation_template().get("current_world_state", {})
        if layered_state.get("entity_states"):
            result = deep_copy(layered_state.get("entity_states", {}))
            for character in self.character_db.get("characters", []):
                character_id = character["character_id"]
                result.setdefault(
                    character_id,
                    {
                        "entity_id": character_id,
                        "entity_type": "Character",
                        "name": character["canonical_name"],
                        "record_status": "known_in_source",
                        "mutable_fields": {},
                        "last_updated_by_event_id": None,
                    },
                )
            return result
        result = deep_copy(
            self.world_db.get("world_state", {}).get("entity_states", {})
        )
        for character in self.character_db.get("characters", []):
            character_id = character["character_id"]
            result.setdefault(
                character_id,
                {
                    "entity_id": character_id,
                    "entity_type": "Character",
                    "name": character["canonical_name"],
                    "record_status": "known_in_source",
                    "mutable_fields": {},
                    "last_updated_by_event_id": None,
                },
            )
        return result

    def _base_resource_states(self):
        layered_state = self._simulation_template().get("current_world_state", {})
        return deep_copy(layered_state.get("resource_states", {}))

    def _base_relationship_states(self):
        layered_state = self._simulation_template().get("current_world_state", {})
        states = deep_copy(layered_state.get("relationship_states", {}))
        cutoff_order = self._simulation_template().get("cutoff_order")
        arc_db = (
            self.world_db.get("relationship_arc_db")
            or self.world_db.get("relationship_system", {}).get(
                "relationship_arc_db", {}
            )
        )
        for arc in arc_db.get("relationship_arcs", []):
            eligible_events = []
            for event in arc.get("arc_events", []):
                try:
                    order = int(event.get("source_chunk_id"))
                except (TypeError, ValueError):
                    order = None
                if cutoff_order is None or order is None or order <= cutoff_order:
                    eligible_events.append(event)
            if not eligible_events:
                continue
            states.setdefault(
                arc["relationship_arc_id"],
                {
                    "relationship_id": arc["relationship_arc_id"],
                    "participant_ids": arc.get("participant_ids", []),
                    "participant_names": arc.get("participant_names", []),
                    "status": arc.get(
                        "current_status", "established_from_relationship_arc"
                    ),
                    "current_value": eligible_events[-1].get(
                        "relationship_type", "related"
                    ),
                    "first_seen_order": eligible_events[0].get(
                        "source_chunk_id"
                    ),
                    "last_updated_by_event_id": None,
                    "evidence_refs": eligible_events[-12:],
                    "source": "relationship_arc_db",
                },
            )
        return states

    def _base_identity_states(self):
        layered_state = self._simulation_template().get("current_world_state", {})
        return deep_copy(layered_state.get("identity_states", {}))

    def _base_runtime_events(self):
        return deep_copy(self.world_db.get("runtime_event_db", {}))

    def _base_runtime_relationship_db(self):
        if self.world_db.get("runtime_relationship_db"):
            return deep_copy(self.world_db["runtime_relationship_db"])
        return build_runtime_relationship_db(
            self._simulation_template(),
            self.world_db.get("canonical_relationship_db", {}),
        )

    def _base_runtime_agent_state(self):
        return build_runtime_agent_state(
            self.agent_profiles,
            self._simulation_template(),
            self._base_runtime_relationship_db(),
        )

    def _base_runtime_log(self):
        if self.world_db.get("runtime_log"):
            return deep_copy(self.world_db["runtime_log"])
        return build_runtime_log(
            self._simulation_template(),
            self._base_runtime_events(),
        )

    def _base_ownership(self):
        ownership = {}
        resource_states = self._base_resource_states()
        for resource in resource_states.values():
            if resource.get("resource_type") != "artifact":
                continue
            holder_ids = (
                resource.get("current_holder_ids")
                or resource.get("current_owner_ids")
                or resource.get("current_user_ids")
            )
            holder_id = holder_ids[0] if holder_ids else None
            ownership[resource["resource_id"]] = {
                "artifact_id": resource["resource_id"],
                "holder_id": holder_id,
                "status": resource.get("status", "available"),
                "location_id": None,
                "source": "simulation_state_db",
                "original_owner_ids": resource.get("original_owner_ids", []),
                "canonical_owner_ids": resource.get("canonical_owner_ids", []),
            }
        if ownership:
            return ownership
        for agent in self.agent_profiles.get("agents", []):
            for item in agent.get("capabilities", {}).get("owned_items", []):
                ownership[item["entity_id"]] = {
                    "artifact_id": item["entity_id"],
                    "holder_id": agent["character_id"],
                    "status": "available",
                    "location_id": None,
                    "source": "agent_profile_baseline",
                }
        return ownership

    def _new_branch(self, branch_id, label, parent_branch_id=None):
        character_runtime = {
            item["character_id"]: {
                "character_id": item["character_id"],
                **deep_copy(RUNTIME_CHARACTER_DEFAULTS),
                "long_term_goal": clean_text(
                    "；".join(
                        clean_text(goal)
                        for goal in item.get("goals", [])
                        if clean_text(goal)
                    )
                ),
            }
            for item in self.character_db.get("characters", [])
        }
        motivation_runtime = {
            item["character_id"]: {
                "character_id": item["character_id"],
                "dominant_drive": "",
                "active_objective": "",
                "active_fear": "",
                "current_strategy": "",
                "desire_intensity": 0,
                "fear_intensity": 0,
                "attachment_focus": "",
                "temptation_focus": "",
                "disguise_pressure": 0,
                "action_policy": {
                    "priority": "根本欲望决定方向；恐惧只改变路线、节奏和伪装强度。",
                    "when_threatened": "采取保守、试探或隐蔽推进，不能把停滞当作长期目标。",
                    "stall_guard": "除非玩家明确要求或角色被外力限制，不要整轮只屏息、僵住、装死或扮作无生命物。",
                    "action_bias": "goal_directed_survival",
                    "forward_actions": ["观察", "试探", "换策略", "保留下一步机会"],
                },
                "confidence": "unknown",
                "last_trigger": "",
                "last_updated_by_event_id": None,
                "history": [],
            }
            for item in self.character_db.get("characters", [])
        }
        baseline = {
            "entity_states": self._base_entity_states(),
            "resource_states": self._base_resource_states(),
            "artifact_states": self._base_ownership(),
            "relationship_states": self._base_relationship_states(),
            "identity_states": self._base_identity_states(),
            "knowledge_ledger": {},
            "active_scene": None,
            "agent_memories": {},
            "conversation_log": [],
            "recent_dialogue_turns": [],
            "recovery_snapshot": {},
            "guardrail_incidents": [],
            "simulation_clock": {
                "era": "Story Era",
                "day": 1,
                "minute_of_day": 480,
                "elapsed_minutes": 0,
            },
            "engine": {
                "status": "paused",
                "speed": 1,
                "last_tick_at": None,
            },
            "agent_control": {},
            "backend_log": [],
            "pending_actions": [],
            "character_runtime": character_runtime,
            "motivation_runtime": motivation_runtime,
            "location_runtime": {},
            "active_events": [],
            "runtime_event_db": self._base_runtime_events(),
            "runtime_event_queue": self._base_runtime_events().get(
                "event_queue", []
            ),
            "runtime_relationship_db": self._base_runtime_relationship_db(),
            "runtime_agent_state": self._base_runtime_agent_state(),
            "runtime_agent_knowledge_dbs": {},
            "runtime_log": self._base_runtime_log(),
            "canonical_timeline": [],
            "timeline_cursor": 0,
            "narrative_spine": {
                "status": "not_started",
                "current_anchor": {},
                "last_canonical_event_status": "unchanged",
                "last_updated_revision": 0,
                "policy": {
                    "canonical_events_are_pressure_not_script": True,
                    "runtime_branches_may_diverge": True,
                    "timeline_cursor_advances_when_anchor_is_resolved": True,
                },
            },
            "branch_records": [],
            "long_term_memories": {},
            "world_knowledge_cache": {},
            "admin_profile_overrides": {},
            "world_admin_log": [],
        }
        return {
            "branch_id": branch_id,
            "label": label,
            "parent_branch_id": parent_branch_id,
            "created_at": utc_now(),
            "head_revision": 0,
            "baseline": deep_copy(baseline),
            "runtime": deep_copy(baseline),
            "events": [],
            "committed_event_ids": [],
            "idempotency_keys": {},
            "checkpoints": [{"revision": 0, "label": "baseline", "created_at": utc_now()}],
        }

    def _new_state(self):
        branch = self._new_branch("main", "Main")
        return {
            "schema_version": STEP17_SCHEMA_VERSION,
            "purpose": "Mutable simulation state; world_db.json remains read-only.",
            "world_db_fingerprint": self.world_fingerprint,
            "agent_profile_db_fingerprint": self.agent_fingerprint,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "active_branch_id": "main",
            "branches": {"main": branch},
        }

    def _load_or_create(self):
        if not self.path.exists():
            state = self._new_state()
            atomic_write_json(self.path, state)
            return state
        state = json.loads(self.path.read_text(encoding="utf-8"))
        if state.get("schema_version") != STEP17_SCHEMA_VERSION:
            raise ValueError("simulation_state.json schema does not match Step 17.")
        if state.get("world_db_fingerprint") != self.world_fingerprint:
            backup = self.path.with_suffix(
                self.path.suffix + "." + utc_now().replace(":", "-") + ".bak"
            )
            atomic_write_json(backup, state)
            state = self._new_state()
            state["superseded_state_path"] = str(backup)
            atomic_write_json(self.path, state)
            return state
        if state.get("agent_profile_db_fingerprint") != self.agent_fingerprint:
            backup = self.path.with_suffix(
                self.path.suffix + "." + utc_now().replace(":", "-") + ".bak"
            )
            atomic_write_json(backup, state)
            state = self._new_state()
            state["superseded_state_path"] = str(backup)
            state["superseded_reason"] = "agent_profile_db_fingerprint_changed"
            atomic_write_json(self.path, state)
            return state
        changed = False
        for branch in state.get("branches", {}).values():
            for target_name in ("baseline", "runtime"):
                target = branch[target_name]
                defaults = self._new_branch("_migration", "_migration")[
                    "baseline"
                ]
                for key in (
                    "simulation_clock",
                    "engine",
                    "agent_control",
                    "backend_log",
                    "pending_actions",
                    "character_runtime",
                    "motivation_runtime",
                    "location_runtime",
                    "active_events",
                    "resource_states",
                    "identity_states",
                    "runtime_event_db",
                    "runtime_event_queue",
                    "runtime_relationship_db",
                    "runtime_agent_state",
                    "runtime_agent_knowledge_dbs",
                    "runtime_log",
                    "canonical_timeline",
                    "timeline_cursor",
                    "narrative_spine",
                    "branch_records",
                    "long_term_memories",
                    "world_knowledge_cache",
                    "recent_dialogue_turns",
                    "recovery_snapshot",
                    "admin_profile_overrides",
                    "world_admin_log",
                ):
                    if key not in target:
                        target[key] = deep_copy(defaults[key])
                        changed = True
        if changed:
            atomic_write_json(self.path, state)
        return state

    @property
    def branch(self):
        return self.state["branches"][self.state["active_branch_id"]]

    @property
    def runtime(self):
        return self.branch["runtime"]

    def _runtime_as_template(self):
        template = deep_copy(self._simulation_template())
        template.setdefault("current_world_state", {})
        template["current_world_state"].update(
            {
                "entity_states": deep_copy(self.runtime.get("entity_states", {})),
                "resource_states": deep_copy(self.runtime.get("resource_states", {})),
                "identity_states": deep_copy(self.runtime.get("identity_states", {})),
                "relationship_states": deep_copy(
                    self.runtime.get("relationship_states", {})
                ),
                "state_revision": self.branch.get("head_revision", 0),
                "branch_id": self.branch.get("branch_id", "main"),
            }
        )
        return template

    def _refresh_runtime_sidecars(self, committed_event=None):
        runtime_template = self._runtime_as_template()
        relationship_db = build_runtime_relationship_db(
            runtime_template,
            self.world_db.get("canonical_relationship_db", {}),
        )
        relationship_db["change_log"] = deep_copy(
            self.runtime.get("runtime_relationship_db", {}).get("change_log", [])
        )
        if committed_event:
            for patch in committed_event.get("patches", []):
                if patch.get("field") == "relationship":
                    relationship_db["change_log"].append(
                        {
                            "event_id": committed_event["event_id"],
                            "revision": committed_event["revision_after"],
                            "patch": deep_copy(patch),
                        }
                    )
        self.runtime["runtime_relationship_db"] = relationship_db

        event_db = deep_copy(self.runtime.get("runtime_event_db", {}))
        if committed_event:
            event_db.setdefault("runtime_committed_events", []).append(
                {
                    "event_id": committed_event["event_id"],
                    "event_type": committed_event.get("event_type"),
                    "revision": committed_event["revision_after"],
                    "participants": committed_event.get("participants", []),
                    "state_change_count": len(committed_event.get("patches", [])),
                    "created_at": committed_event.get("created_at", utc_now()),
                }
            )
            for queued in event_db.get("event_queue", []):
                if queued.get("runtime_event_id") == committed_event.get(
                    "runtime_event_id"
                ) or queued.get("canonical_event_id") == committed_event.get(
                    "canonical_event_id"
                ):
                    queued["status"] = "completed"
                    queued["queue_status"] = "completed"
                    queued["committed_at_revision"] = committed_event[
                        "revision_after"
                    ]
        event_db["completed_event_ids"] = [
            item.get("runtime_event_id")
            for item in event_db.get("event_queue", [])
            if item.get("queue_status") == "completed"
        ]
        event_db["waiting_trigger_event_ids"] = [
            item.get("runtime_event_id")
            for item in event_db.get("event_queue", [])
            if item.get("queue_status") == "waiting_trigger"
        ]
        event_db["active_event_ids"] = [
            item.get("runtime_event_id")
            for item in event_db.get("event_queue", [])
            if item.get("queue_status") == "active"
        ]
        self.runtime["runtime_event_db"] = event_db
        self.runtime["runtime_event_queue"] = deep_copy(
            event_db.get("event_queue", [])
        )

        self.runtime["runtime_agent_state"] = build_runtime_agent_state(
            self.agent_profiles,
            runtime_template,
            relationship_db,
        )
        for agent_state in self.runtime["runtime_agent_state"].get(
            "agent_states", {}
        ).values():
            character_id = agent_state.get("character_id")
            memory = self.runtime.get("agent_memories", {}).get(
                character_id, {}
            )
            agent_state["short_term_memory"] = deep_copy(
                memory.get("recent_event_ids", [])
            )
            agent_state["memory_summary"] = clean_text(
                memory.get("summary")
            )
            agent_state["memory_last_revision"] = memory.get(
                "last_revision", 0
            )
            agent_state["motivation_runtime"] = deep_copy(
                self.runtime.get("motivation_runtime", {}).get(
                    character_id, {}
                )
            )

        runtime_log = deep_copy(self.runtime.get("runtime_log") or {})
        runtime_log.setdefault("schema_version", STEP17_SCHEMA_VERSION)
        runtime_log.setdefault("layer", "Runtime Log")
        runtime_log.setdefault("entries", [])
        if committed_event:
            runtime_log["entries"].append(
                {
                    "log_id": "log_" + committed_event["event_id"],
                    "entry_type": committed_event.get("event_type", "runtime_event"),
                    "event_id": committed_event["event_id"],
                    "revision": committed_event["revision_after"],
                    "branch_id": self.branch.get("branch_id"),
                    "participants": committed_event.get("participants", []),
                    "state_change_count": len(committed_event.get("patches", [])),
                    "created_at": committed_event.get("created_at", utc_now()),
                }
            )
        self.runtime["runtime_log"] = runtime_log

    def _sync_sidecar_files(self):
        if not self.runtime_dir:
            return
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            self.runtime_dir / "runtime_event_db.json",
            self.runtime.get("runtime_event_db", {}),
        )
        atomic_write_json(
            self.runtime_dir / "runtime_relationship_db.json",
            self.runtime.get("runtime_relationship_db", {}),
        )
        atomic_write_json(
            self.runtime_dir / "runtime_log.json",
            self.runtime.get("runtime_log", {}),
        )
        atomic_write_json(
            self.agents_dir / "runtime_agent_state.json",
            self.runtime.get("runtime_agent_state", {}),
        )
        atomic_write_json(
            self.agents_dir / "runtime_motivation_state.json",
            self.runtime.get("motivation_runtime", {}),
        )
        agent_db_dir = self.agents_dir / "runtime_agent_dbs"
        agent_db_dir.mkdir(parents=True, exist_ok=True)
        agent_db_index = {
            "schema_version": STEP17_SCHEMA_VERSION,
            "layer": "Runtime Agent Knowledge DB Index",
            "agent_count": len(
                self.runtime.get("runtime_agent_knowledge_dbs", {})
            ),
            "agents": [],
        }
        for character_id, agent_db in self.runtime.get(
            "runtime_agent_knowledge_dbs", {}
        ).items():
            safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(character_id))
            filename = f"{safe_id}.json"
            agent_db_index["agents"].append(
                {
                    "character_id": character_id,
                    "canonical_name": agent_db.get("canonical_name", ""),
                    "runtime_access_tier": agent_db.get(
                        "runtime_access_tier", "cold_reference"
                    ),
                    "path": f"runtime_agent_dbs/{filename}",
                    "updated_revision": agent_db.get("updated_revision", 0),
                }
            )
            atomic_write_json(agent_db_dir / filename, agent_db)
        atomic_write_json(
            self.agents_dir / "runtime_agent_dbs_index.json",
            agent_db_index,
        )

    def save(self):
        self._refresh_runtime_sidecars()
        self.state["updated_at"] = utc_now()
        atomic_write_json(self.path, self.state)
        self._sync_sidecar_files()

    def reset(self):
        self.state = self._new_state()
        self.save()
        return self.snapshot()

    def snapshot(self):
        clock = deep_copy(self.runtime.get("simulation_clock", {}))
        engine = deep_copy(self.runtime.get("engine", {}))
        return {
            "branch_id": self.branch["branch_id"],
            "revision": self.branch["head_revision"],
            "active_scene": deep_copy(self.runtime.get("active_scene")),
            "event_count": len(self.branch["events"]),
            "clock": clock,
            "engine": engine,
            "pending_action_count": len(
                self.runtime.get("pending_actions", [])
            ),
            "active_agent_count": len(
                (self.runtime.get("active_scene") or {}).get(
                    "participant_ids", []
                )
            ),
        }

    def _commit_system_event(self, event_type, **payload):
        event_id = "event_" + uuid.uuid4().hex[:16]
        event = {
            "event_id": event_id,
            "idempotency_key": payload.pop(
                "idempotency_key",
                f"{event_type}:{uuid.uuid4().hex}",
            ),
            "event_type": event_type,
            "impact_level": "minor_action",
            "status": "completed",
            "participants": payload.pop("participants", []),
            "visible_to": payload.pop("visible_to", []),
            "narration": clean_text(payload.pop("narration", "")),
            "dialogue": [],
            "action_intents": [],
            "state_changes": [],
            "evidence_refs": [],
            "created_at": utc_now(),
            **payload,
        }
        decision = {
            "status": "allowed",
            "commit_allowed": True,
            "checks": [],
            "user_visible_reason": "",
        }
        return self.commit_event(event, decision)

    @staticmethod
    def _merge_runtime_updates(runtime, updates):
        replace_keys = set((updates or {}).get("__replace_keys__", []))
        for key, value in (updates or {}).items():
            if key == "__replace_keys__":
                continue
            if key in replace_keys:
                runtime[key] = deep_copy(value)
                continue
            if isinstance(value, dict) and isinstance(runtime.get(key), dict):
                for nested_key, nested_value in value.items():
                    if (
                        isinstance(nested_value, dict)
                        and isinstance(runtime[key].get(nested_key), dict)
                    ):
                        runtime[key][nested_key].update(
                            deep_copy(nested_value)
                        )
                    else:
                        runtime[key][nested_key] = deep_copy(nested_value)
            else:
                runtime[key] = deep_copy(value)

    def set_engine(self, status=None, speed=None):
        current = self.runtime.get("engine", {})
        transition = {
            "status": status or current.get("status", "paused"),
            "speed": int(speed or current.get("speed", 1)),
            "last_tick_at": utc_now(),
        }
        return self._commit_system_event(
            "engine_control_changed",
            engine_transition=transition,
            backend_stage="engine_control",
        )

    def set_agent_control(self, character_id, mode):
        mode = clean_text(mode).upper()
        if mode not in {"AUTO", "ASSISTED", "MANUAL"}:
            raise ValueError("Agent control must be AUTO, ASSISTED, or MANUAL.")
        scene = deep_copy(self.runtime.get("active_scene"))
        changes = {character_id: mode}
        if mode == "MANUAL" and scene:
            previous_focus = scene.get("focus_character_id")
            if previous_focus and previous_focus != character_id:
                changes[previous_focus] = "AUTO"
            scene["focus_character_id"] = character_id
        return self._commit_system_event(
            "agent_control_changed",
            participants=[character_id],
            agent_control_changes=changes,
            scene_transition=scene,
            backend_stage="agent_control",
        )

    def advance_time(self, minutes, reason="manual_time_advance"):
        minutes = max(1, int(minutes))
        clock = deep_copy(self.runtime.get("simulation_clock", {}))
        total = int(clock.get("minute_of_day", 480)) + minutes
        clock["day"] = int(clock.get("day", 1)) + total // 1440
        clock["minute_of_day"] = total % 1440
        clock["elapsed_minutes"] = int(clock.get("elapsed_minutes", 0)) + minutes
        scene = self.runtime.get("active_scene") or {}
        return self._commit_system_event(
            "world_time_advanced",
            participants=scene.get("participant_ids", []),
            visible_to=scene.get("participant_ids", []),
            narration=f"世界时间推进 {minutes} 分钟。",
            clock_transition=clock,
            backend_stage=reason,
        )

    def clock_after_minutes(self, minutes):
        minutes = max(0, int(minutes or 0))
        clock = deep_copy(self.runtime.get("simulation_clock", {}))
        total = int(clock.get("minute_of_day", 480)) + minutes
        clock["day"] = int(clock.get("day", 1)) + total // 1440
        clock["minute_of_day"] = total % 1440
        clock["elapsed_minutes"] = int(clock.get("elapsed_minutes", 0)) + minutes
        return clock

    def resolve_pending_action(self, pending_id, accepted):
        pending = self.runtime.get("pending_actions", [])
        remaining = [
            item for item in pending if item.get("pending_id") != pending_id
        ]
        return self._commit_system_event(
            "assisted_action_resolved",
            pending_actions_after=remaining,
            backend_stage=(
                "assisted_action_accepted"
                if accepted
                else "assisted_action_rejected"
            ),
        )

    def start_scene(
        self,
        focus_character_id,
        participant_ids,
        location_id=None,
        scene_summary="",
    ):
        participants = [
            item
            for item in dict.fromkeys([focus_character_id, *participant_ids])
            if clean_text(item)
        ]
        scene_id = "scene_" + uuid.uuid4().hex[:16]
        event = {
            "event_id": "event_" + uuid.uuid4().hex[:16],
            "idempotency_key": "start_scene:" + scene_id,
            "event_type": "scene_started",
            "impact_level": "minor_action",
            "status": "completed",
            "participants": participants,
            "visible_to": participants,
            "narration": clean_text(scene_summary),
            "dialogue": [],
            "action_intents": [],
            "state_changes": [
                {
                    "subject_id": character_id,
                    "field": "presence",
                    "before": "unknown",
                    "after": "present",
                }
                for character_id in participants
            ],
            "scene_transition": {
                "scene_id": scene_id,
                "focus_character_id": focus_character_id,
                "participant_ids": participants,
                "location_id": location_id,
                "summary": clean_text(scene_summary),
                "turn": 0,
            },
            "evidence_refs": [],
            "created_at": utc_now(),
        }
        decision = {
            "status": "allowed",
            "commit_allowed": True,
            "checks": [],
            "user_visible_reason": "",
        }
        self.commit_event(event, decision)
        return event

    def _apply_change(self, runtime, change, event_id):
        subject_id = clean_text(change.get("subject_id"))
        field = clean_text(change.get("field"))
        if not subject_id or not field:
            raise ValueError("Every state change requires subject_id and field.")
        if field not in GENERIC_EVENT_FIELDS and not field.startswith(
            ("state.", "custom.")
        ):
            raise ValueError(f"Unsupported state field: {field}")

        if field in {"holder_id", "owner_id", "condition", "availability"}:
            record = runtime["artifact_states"].setdefault(
                subject_id,
                {
                    "artifact_id": subject_id,
                    "holder_id": None,
                    "status": "unknown",
                    "location_id": None,
                    "source": "runtime",
                },
            )
            key = "status" if field in {"condition", "availability"} else "holder_id"
            previous = record.get(key, "unknown")
            record[key] = change.get("after")
            resource = runtime.setdefault("resource_states", {}).get(subject_id)
            if resource:
                if key == "holder_id":
                    resource["current_holder_ids"] = [
                        change.get("after")
                    ] if change.get("after") else []
                    resource["current_owner_ids"] = [
                        change.get("after")
                    ] if change.get("after") else []
                else:
                    resource["status"] = change.get("after")
                resource["last_updated_by_event_id"] = event_id
        elif field in {
            "current_owner_ids",
            "current_user_ids",
            "current_holder_ids",
            "resource_status",
            "acquired_by",
            "released_by",
        }:
            resource = runtime.setdefault("resource_states", {}).setdefault(
                subject_id,
                {
                    "resource_id": subject_id,
                    "resource_type": change.get("resource_type", "runtime_resource"),
                    "canonical_name": change.get("subject_name", subject_id),
                    "access_type": change.get("access_type", "open"),
                    "original_owner_ids": change.get("original_owner_ids", []),
                    "canonical_owner_ids": change.get("canonical_owner_ids", []),
                    "current_owner_ids": [],
                    "current_user_ids": [],
                    "current_holder_ids": [],
                    "status": "runtime_created",
                    "last_updated_by_event_id": None,
                },
            )
            if field == "resource_status":
                previous = resource.get("status", "unknown")
                resource["status"] = change.get("after")
            elif field == "acquired_by":
                owner_ids = list(resource.get("current_owner_ids", []))
                previous = "acquired" if change.get("after") in owner_ids else "unknown"
                if change.get("after") and change.get("after") not in owner_ids:
                    owner_ids.append(change.get("after"))
                resource["current_owner_ids"] = owner_ids
                if resource.get("resource_type") == "artifact":
                    resource["current_holder_ids"] = owner_ids
            elif field == "released_by":
                owner_ids = [
                    item
                    for item in resource.get("current_owner_ids", [])
                    if item != change.get("after")
                ]
                previous = "acquired" if owner_ids != resource.get("current_owner_ids", []) else "unknown"
                resource["current_owner_ids"] = owner_ids
                if resource.get("resource_type") == "artifact":
                    resource["current_holder_ids"] = owner_ids
            else:
                previous = deep_copy(resource.get(field, []))
                value = change.get("after")
                resource[field] = value if isinstance(value, list) else [value]
            resource["last_updated_by_event_id"] = event_id
        elif field == "relationship":
            relation_key = clean_text(change.get("relation_key")) or subject_id
            previous = runtime["relationship_states"].get(relation_key, "unknown")
            runtime["relationship_states"][relation_key] = change.get("after")
        elif field == "knowledge":
            character_id = clean_text(change.get("character_id")) or subject_id
            ledger = runtime["knowledge_ledger"].setdefault(character_id, [])
            previous = "known" if change.get("after") in ledger else "unknown"
            if change.get("after") not in ledger:
                ledger.append(change.get("after"))
        else:
            entity = runtime["entity_states"].setdefault(
                subject_id,
                {
                    "entity_id": subject_id,
                    "entity_type": change.get("subject_type", "RuntimeEntity"),
                    "name": change.get("subject_name", subject_id),
                    "record_status": "runtime_created",
                    "mutable_fields": {},
                    "last_updated_by_event_id": None,
                },
            )
            key = field.split(".", 1)[1] if field.startswith("state.") else field
            previous = entity["mutable_fields"].get(key, "unknown")
            entity["mutable_fields"][key] = change.get("after")
            entity["last_updated_by_event_id"] = event_id

        expected = change.get("before", "unknown")
        if expected not in {"unknown", previous}:
            raise ValueError(
                f"State precondition failed for {subject_id}.{field}: {previous}"
            )
        return {
            "subject_id": subject_id,
            "field": field,
            "before": previous,
            "after": change.get("after"),
        }

    def commit_event(self, event, validation):
        if validation.get("status") != "allowed" or not validation.get(
            "commit_allowed"
        ):
            raise ValueError("Only an allowed validated event may modify state.")
        event_id = clean_text(event.get("event_id"))
        idempotency_key = clean_text(event.get("idempotency_key"))
        if not event_id or not idempotency_key:
            raise ValueError("Event requires event_id and idempotency_key.")
        previous_event_id = self.branch["idempotency_keys"].get(idempotency_key)
        if previous_event_id:
            return {
                "status": "duplicate_ignored",
                "event_id": previous_event_id,
                "revision": self.branch["head_revision"],
            }
        if event_id in self.branch["committed_event_ids"]:
            return {
                "status": "duplicate_ignored",
                "event_id": event_id,
                "revision": self.branch["head_revision"],
            }

        runtime_copy = deep_copy(self.runtime)
        patches = [
            self._apply_change(runtime_copy, change, event_id)
            for change in event.get("state_changes", [])
        ]
        if event.get("scene_transition"):
            runtime_copy["active_scene"] = deep_copy(event["scene_transition"])
        elif runtime_copy.get("active_scene"):
            runtime_copy["active_scene"]["turn"] = (
                runtime_copy["active_scene"].get("turn", 0) + 1
            )
        if event.get("clock_transition"):
            runtime_copy["simulation_clock"] = deep_copy(
                event["clock_transition"]
            )
        if event.get("engine_transition"):
            runtime_copy["engine"] = deep_copy(event["engine_transition"])
        for character_id, mode in event.get(
            "agent_control_changes", {}
        ).items():
            runtime_copy["agent_control"][character_id] = mode
        runtime_copy["pending_actions"] = deep_copy(
            event.get(
                "pending_actions_after",
                runtime_copy.get("pending_actions", []),
            )
        )
        self._merge_runtime_updates(
            runtime_copy, event.get("runtime_updates", {})
        )
        runtime_copy["backend_log"].append(
            {
                "event_id": event_id,
                "revision": self.branch["head_revision"] + 1,
                "stage": event.get("backend_stage", event.get("event_type")),
                "world_agent": bool(event.get("world_projection")),
                "state_change_count": len(event.get("state_changes", [])),
                "created_at": event.get("created_at", utc_now()),
            }
        )
        runtime_copy["backend_log"] = runtime_copy["backend_log"][-200:]

        for line in event.get("dialogue", []):
            runtime_copy["conversation_log"].append(
                {
                    "event_id": event_id,
                    "speaker_id": line.get("speaker_id"),
                    "text": clean_text(line.get("text")),
                    "created_at": event.get("created_at", utc_now()),
                }
            )
        player_input = clean_text(event.get("player_input"))
        if player_input or clean_text(event.get("narration")):
            runtime_copy["recent_dialogue_turns"] = [
                *runtime_copy.get("recent_dialogue_turns", []),
                {
                    "event_id": event_id,
                    "revision": self.branch["head_revision"] + 1,
                    "player_id": event.get("player_id"),
                    "player_input": player_input,
                    "narration": clean_text(event.get("narration")),
                    "dialogue": deep_copy(event.get("dialogue", [])),
                    "participants": deep_copy(
                        event.get("participants", [])
                    ),
                    "visible_to": deep_copy(event.get("visible_to", [])),
                    "created_at": event.get("created_at", utc_now()),
                },
            ][-8:]
        runtime_copy["conversation_log"] = runtime_copy[
            "conversation_log"
        ][-40:]
        for participant_id in event.get("participants", []):
            memory = runtime_copy["agent_memories"].setdefault(
                participant_id,
                {
                    "recent_event_ids": [],
                    "summary": "",
                    "last_revision": 0,
                },
            )
            memory["recent_event_ids"] = compact_list(
                [*memory["recent_event_ids"], event_id],
                24,
            )
            memory["last_revision"] = self.branch["head_revision"] + 1

        revision_before = self.branch["head_revision"]
        revision_after = revision_before + 1
        committed = {
            **deep_copy(event),
            "revision_before": revision_before,
            "revision_after": revision_after,
            "patches": patches,
            "validation_summary": {
                "status": validation["status"],
                "check_outcomes": [
                    {
                        "category": item["category"],
                        "outcome": item["outcome"],
                    }
                    for item in validation.get("checks", [])
                ],
            },
        }
        self.branch["runtime"] = runtime_copy
        self.branch["head_revision"] = revision_after
        self.branch["events"].append(committed)
        self.branch["committed_event_ids"].append(event_id)
        self.branch["idempotency_keys"][idempotency_key] = event_id
        self.branch["checkpoints"].append(
            {
                "revision": revision_after,
                "label": event.get("event_type", "event"),
                "created_at": utc_now(),
            }
        )
        self._refresh_runtime_sidecars(committed)
        self.save()
        return {
            "status": "committed",
            "event_id": event_id,
            "revision": revision_after,
            "patches": patches,
        }

    def _replay_to_revision(self, branch, revision):
        runtime = deep_copy(branch["baseline"])
        for event in branch["events"]:
            if event["revision_after"] > revision:
                break
            for patch in event.get("patches", []):
                replay_change = {
                    "subject_id": patch["subject_id"],
                    "field": patch["field"],
                    "before": "unknown",
                    "after": patch["after"],
                }
                self._apply_change(runtime, replay_change, event["event_id"])
            if event.get("scene_transition"):
                runtime["active_scene"] = deep_copy(event["scene_transition"])
            elif runtime.get("active_scene"):
                runtime["active_scene"]["turn"] = (
                    runtime["active_scene"].get("turn", 0) + 1
                )
            if event.get("clock_transition"):
                runtime["simulation_clock"] = deep_copy(
                    event["clock_transition"]
                )
            if event.get("engine_transition"):
                runtime["engine"] = deep_copy(event["engine_transition"])
            for character_id, mode in event.get(
                "agent_control_changes", {}
            ).items():
                runtime["agent_control"][character_id] = mode
            runtime["pending_actions"] = deep_copy(
                event.get(
                    "pending_actions_after",
                    runtime.get("pending_actions", []),
                )
            )
            self._merge_runtime_updates(
                runtime, event.get("runtime_updates", {})
            )
            runtime["backend_log"].append(
                {
                    "event_id": event["event_id"],
                    "revision": event["revision_after"],
                    "stage": event.get(
                        "backend_stage", event.get("event_type")
                    ),
                    "world_agent": bool(event.get("world_projection")),
                    "state_change_count": len(
                        event.get("state_changes", [])
                    ),
                    "created_at": event.get("created_at"),
                }
            )
            runtime["conversation_log"].extend(
                {
                    "event_id": event["event_id"],
                    "speaker_id": line.get("speaker_id"),
                    "text": line.get("text", ""),
                    "created_at": event.get("created_at"),
                }
                for line in event.get("dialogue", [])
            )
            for participant_id in event.get("participants", []):
                memory = runtime["agent_memories"].setdefault(
                    participant_id,
                    {
                        "recent_event_ids": [],
                        "summary": "",
                        "last_revision": 0,
                    },
                )
                memory["recent_event_ids"] = compact_list(
                    [*memory["recent_event_ids"], event["event_id"]],
                    24,
                )
                memory["last_revision"] = event["revision_after"]
        return runtime

    def rollback(self, revision):
        revision = int(revision)
        if revision < 0 or revision > self.branch["head_revision"]:
            raise ValueError("Rollback revision is outside the current branch.")
        self.branch["runtime"] = self._replay_to_revision(self.branch, revision)
        self.branch["events"] = [
            event
            for event in self.branch["events"]
            if event["revision_after"] <= revision
        ]
        self.branch["head_revision"] = revision
        self.branch["committed_event_ids"] = [
            event["event_id"] for event in self.branch["events"]
        ]
        self.branch["idempotency_keys"] = {
            event["idempotency_key"]: event["event_id"]
            for event in self.branch["events"]
        }
        self.branch["checkpoints"] = [
            item
            for item in self.branch["checkpoints"]
            if item["revision"] <= revision
        ]
        self.save()
        return self.snapshot()

    def fork(self, label):
        parent = self.branch
        branch_id = "branch_" + uuid.uuid4().hex[:12]
        branch = deep_copy(parent)
        branch["branch_id"] = branch_id
        branch["label"] = clean_text(label) or branch_id
        branch["parent_branch_id"] = parent["branch_id"]
        branch["created_at"] = utc_now()
        self.state["branches"][branch_id] = branch
        self.state["active_branch_id"] = branch_id
        self.save()
        return self.snapshot()

    def switch_branch(self, branch_id):
        if branch_id not in self.state["branches"]:
            raise KeyError(branch_id)
        self.state["active_branch_id"] = branch_id
        self.save()
        return self.snapshot()

    def replay_events(self, branch_id=None, upto_revision=None):
        branch = self.state["branches"][
            branch_id or self.state["active_branch_id"]
        ]
        events = branch["events"]
        if upto_revision is not None:
            events = [
                event
                for event in events
                if event["revision_after"] <= int(upto_revision)
            ]
        return deep_copy(events)

    def compare_branches(self, left_branch_id, right_branch_id):
        left = self.state["branches"][left_branch_id]
        right = self.state["branches"][right_branch_id]
        left_runtime = left["runtime"]
        right_runtime = right["runtime"]
        changed_entities = []
        entity_ids = set(left_runtime["entity_states"]) | set(
            right_runtime["entity_states"]
        )
        for entity_id in sorted(entity_ids):
            left_state = left_runtime["entity_states"].get(entity_id, {})
            right_state = right_runtime["entity_states"].get(entity_id, {})
            if left_state != right_state:
                changed_entities.append(
                    {
                        "entity_id": entity_id,
                        "left": deep_copy(left_state),
                        "right": deep_copy(right_state),
                    }
                )
        return {
            "left_branch_id": left_branch_id,
            "right_branch_id": right_branch_id,
            "left_revision": left["head_revision"],
            "right_revision": right["head_revision"],
            "left_event_count": len(left["events"]),
            "right_event_count": len(right["events"]),
            "changed_entity_count": len(changed_entities),
            "changed_entities": changed_entities,
            "scene_changed": (
                left_runtime.get("active_scene")
                != right_runtime.get("active_scene")
            ),
        }

    def update_memory_summary(self, character_id, summary):
        memory = self.runtime["agent_memories"].setdefault(
            character_id,
            {"recent_event_ids": [], "summary": "", "last_revision": 0},
        )
        memory["summary"] = clean_text(summary)
        memory["last_revision"] = self.branch["head_revision"]
        self.save()


class WorldValidator:
    """Seven-category validator with one stable output contract."""

    def __init__(self, world_db, character_db, agent_profiles):
        self.world_db = world_db
        self.character_db = character_db
        self.agent_profiles = agent_profiles
        self.character_by_id = character_db.get("character_by_id", {})
        self.profile_by_character_id = {
            item["character_id"]: item for item in agent_profiles.get("agents", [])
        }
        self.concept_candidates = {
            candidate["concept_id"]: candidate
            for record in world_db.get("concept_registry", {}).values()
            for candidate in record.get("candidates", [])
        }

    def _check(self, category, outcome, reason, evidence=None):
        return {
            "category": category,
            "outcome": outcome,
            "internal_reason": clean_text(reason),
            "evidence_refs": evidence or [],
        }

    def _concept_check(self, proposal, scene):
        checks = []
        for reference in proposal.get("concept_refs", []):
            concept_id = clean_text(reference.get("concept_id"))
            surface = clean_text(reference.get("surface"))
            intent = clean_text(reference.get("intent"))
            candidate = self.concept_candidates.get(concept_id)
            if not concept_id or not candidate:
                checks.append(
                    self._check(
                        "concept_resolution",
                        "needs_resolution",
                        f"Unknown concept ID for {surface or 'unnamed reference'}.",
                    )
                )
                continue
            registry = self.world_db.get("concept_registry", {}).get(surface)
            if registry and registry.get("requires_intent") and not intent:
                checks.append(
                    self._check(
                        "concept_resolution",
                        "needs_resolution",
                        f"{surface} requires query intent.",
                    )
                )
                continue
            if candidate.get("model_status") == "rejected":
                outcome = "blocked"
            elif candidate.get("model_status") == "unresolved":
                outcome = "needs_resolution"
            elif not candidate.get("runtime_eligible"):
                outcome = "needs_resolution"
            else:
                outcome = "allowed"
            checks.append(
                self._check(
                    "concept_resolution",
                    outcome,
                    candidate.get("status_reason", candidate.get("model_status")),
                )
            )
        if not proposal.get("concept_refs"):
            checks.append(
                self._check(
                    "concept_resolution",
                    "allowed",
                    "Proposal contains no named world concept requiring resolution.",
                )
            )
        return checks

    def _knowledge_check(self, proposal, actor_id, scene, runtime, rag_ids):
        profile = self.profile_by_character_id.get(actor_id, {})
        known_ids = {
            item.get("concept_id")
            for item in profile.get("world_context", {}).get("knowledge_refs", [])
        }
        known_ids |= set(runtime.get("knowledge_ledger", {}).get(actor_id, []))
        known_ids |= set(rag_ids)
        visible_event_ids = {
            event_id
            for event_id in runtime.get("agent_memories", {})
            .get(actor_id, {})
            .get("recent_event_ids", [])
        }
        checks = []
        for claim in proposal.get("claims", []):
            subject_id = clean_text(claim.get("subject_concept_id"))
            source = clean_text(claim.get("knowledge_source"))
            if subject_id in known_ids or source in {
                "self_background",
                "current_scene",
                "told_by_character",
                "rag",
            }:
                outcome = "allowed"
                reason = "Claim is covered by character knowledge or current retrieval."
            elif clean_text(claim.get("source_event_id")) in visible_event_ids:
                outcome = "allowed"
                reason = "Claim comes from an event visible to the character."
            else:
                outcome = "needs_resolution"
                reason = "Character knowledge does not establish this claim."
            checks.append(self._check("character_knowledge", outcome, reason))
        if not checks:
            checks.append(
                self._check(
                    "character_knowledge",
                    "allowed",
                    "No factual claim requires knowledge validation.",
                )
            )
        return checks

    def _ability_check(self, proposal, actor_id, runtime):
        ability_id = clean_text(
            proposal.get("action_intent", {}).get("ability_concept_id")
        )
        if not ability_id:
            return [
                self._check(
                    "ability",
                    "allowed",
                    "No ability use was proposed.",
                )
            ]
        resource = runtime.get("resource_states", {}).get(ability_id)
        acquisition = (
            self.world_db.get("acquisition_system", {})
            .get("resources", {})
            .get(ability_id, {})
        )
        if not resource:
            return [
                self._check(
                    "ability",
                    "needs_resolution",
                    "Ability is defined canonically but has not been acquired in the current simulation state.",
                    acquisition.get("conditions", {}).get(
                        "acquisition_conditions", []
                    ),
                )
            ]
        current_users = set(resource.get("current_user_ids", []))
        current_owners = set(resource.get("current_owner_ids", []))
        access_type = resource.get("access_type", acquisition.get("access_type", "open"))
        if actor_id not in current_users | current_owners:
            outcome = "blocked" if access_type == "exclusive" else "uncertain"
            return [
                self._check(
                    "ability",
                    outcome,
                    (
                        "Exclusive ability is not currently owned by this actor."
                        if access_type == "exclusive"
                        else "Open ability still requires an acquisition event before use."
                    ),
                    acquisition.get("conditions", {}).get(
                        "acquisition_conditions", []
                    ),
                )
            ]
        state = runtime.get("entity_states", {}).get(ability_id, {})
        available = state.get("mutable_fields", {}).get("availability", "available")
        if available not in {"available", "unknown"}:
            return [
                self._check(
                    "ability",
                    "blocked",
                    f"Ability is currently {available}.",
                )
            ]
        return [
            self._check(
                "ability",
                "allowed",
                "Ability is present in current resource state and availability is compatible.",
                acquisition.get("conditions", {}).get("use_conditions", []),
            )
        ]

    def _artifact_check(self, proposal, actor_id, scene, runtime):
        artifact_id = clean_text(
            proposal.get("action_intent", {}).get("artifact_concept_id")
        )
        if not artifact_id:
            return [
                self._check("artifact", "allowed", "No artifact use was proposed.")
            ]
        resource = runtime.get("resource_states", {}).get(artifact_id)
        acquisition = (
            self.world_db.get("acquisition_system", {})
            .get("resources", {})
            .get(artifact_id, {})
        )
        if not resource:
            return [
                self._check(
                    "artifact",
                    "needs_resolution",
                    "Artifact has no current resource state; it must be found, received, created, or otherwise acquired by event.",
                    acquisition.get("conditions", {}).get(
                        "acquisition_conditions", []
                    ),
                )
            ]
        record = runtime.get("artifact_states", {}).get(artifact_id)
        if not record:
            return [
                self._check(
                    "artifact",
                    "needs_resolution",
                    "Artifact exists in the world model but has no runtime custody state.",
                )
            ]
        current_holders = set(
            resource.get("current_holder_ids")
            or resource.get("current_owner_ids")
            or []
        )
        if current_holders and actor_id not in current_holders:
            return [
                self._check(
                    "artifact",
                    "blocked",
                    "Artifact current holder in Simulation State DB is another entity.",
                    acquisition.get("conditions", {}).get("use_conditions", []),
                )
            ]
        if record.get("status") not in {"available", "unknown"}:
            return [
                self._check(
                    "artifact",
                    "blocked",
                    f"Artifact status is {record.get('status')}.",
                )
            ]
        if record.get("holder_id") not in {None, actor_id}:
            return [
                self._check(
                    "artifact",
                    "blocked",
                    "Artifact is held by another entity.",
                )
            ]
        location_id = scene.get("location_id") if scene else None
        if (
            record.get("location_id")
            and location_id
            and record["location_id"] != location_id
        ):
            return [
                self._check(
                    "artifact",
                    "blocked",
                    "Artifact is not present in the current scene.",
                )
            ]
        return [
            self._check(
                "artifact",
                "allowed",
                "Artifact custody, condition, current resource owner, and scene presence are compatible.",
                acquisition.get("conditions", {}).get("use_conditions", []),
            )
        ]

    def _rule_check(self, proposal, actor_id):
        action = proposal.get("action_intent", {})
        targets = action.get("target_concept_ids", [])
        candidate_rules = action.get("candidate_rule_ids", [])
        rules = []
        for rule in self.world_db.get("rule_engine", {}).get("rules", []):
            entity_match = (
                actor_id in rule.get("constrains", [])
                or actor_id in rule.get("applies_to", [])
                or bool(set(targets) & set(rule.get("constrains", [])))
                or bool(set(targets) & set(rule.get("applies_to", [])))
            )
            if rule["rule_id"] in candidate_rules or entity_match:
                rules.append(rule)
        impact = action.get("impact_level", "dialogue")
        if not rules:
            if impact in NON_STATEFUL_IMPACT_LEVELS:
                return [
                    self._check(
                        "world_rule",
                        "allowed",
                        "No applicable rule; non-stateful action may proceed.",
                    )
                ]
            return [
                self._check(
                    "world_rule",
                    "uncertain",
                    "No applicable rule; stateful result requires GM adjudication.",
                )
            ]
        if any(
            rule["model_status"] == "trusted"
            and rule["enforcement"] == "hard_block"
            for rule in rules
        ):
            outcome = "blocked"
        elif any(rule["model_status"] == "supported" for rule in rules):
            outcome = "uncertain"
        elif any(rule["enforcement"] == "requires_runtime_review" for rule in rules):
            outcome = "uncertain"
        else:
            outcome = "allowed"
        return [
            self._check(
                "world_rule",
                outcome,
                "Applicable world rules were evaluated.",
                [
                    evidence
                    for rule in rules
                    for evidence in rule.get("evidence", [])
                ],
            )
        ]

    def _time_check(self, proposal, actor_id, scene, runtime):
        participants = set((scene or {}).get("participant_ids", []))
        if scene and actor_id not in participants:
            return [
                self._check(
                    "temporal_consistency",
                    "blocked",
                    "Acting character is not present in the current scene.",
                )
            ]
        actor_state = runtime.get("entity_states", {}).get(actor_id, {})
        actor_location = actor_state.get("mutable_fields", {}).get("location_id")
        scene_location = (scene or {}).get("location_id")
        if actor_location and scene_location and actor_location != scene_location:
            return [
                self._check(
                    "temporal_consistency",
                    "blocked",
                    "Character is recorded at a mutually exclusive location.",
                )
            ]
        future_claim = any(
            claim.get("temporal_scope") == "future"
            and claim.get("knowledge_source") != "prediction"
            for claim in proposal.get("claims", [])
        )
        return [
            self._check(
                "temporal_consistency",
                "blocked" if future_claim else "allowed",
                (
                    "Future event was stated as known fact."
                    if future_claim
                    else "Scene presence and temporal scope are compatible."
                ),
            )
        ]

    def _conflict_check(self, proposal, runtime):
        conflicts = []
        for change in proposal.get("action_intent", {}).get(
            "proposed_state_changes", []
        ):
            subject_id = change.get("subject_id")
            field = change.get("field", "")
            if field in {"holder_id", "owner_id"}:
                current = runtime.get("artifact_states", {}).get(
                    subject_id, {}
                ).get("holder_id", "unknown")
            else:
                key = field.split(".", 1)[1] if field.startswith("state.") else field
                current = runtime.get("entity_states", {}).get(
                    subject_id, {}
                ).get("mutable_fields", {}).get(key, "unknown")
            expected = change.get("before", "unknown")
            if expected not in {"unknown", current}:
                conflicts.append((subject_id, field, current, expected))
        if conflicts:
            return [
                self._check(
                    "fact_conflict",
                    "blocked",
                    f"State precondition conflicts: {conflicts}",
                )
            ]
        return [
            self._check(
                "fact_conflict",
                "allowed",
                "No proposed change contradicts the committed state.",
            )
        ]

    def validate(self, proposal, actor_id, store, rag_ids=None):
        scene = store.runtime.get("active_scene") or {}
        checks = []
        checks.extend(self._concept_check(proposal, scene))
        checks.extend(
            self._knowledge_check(
                proposal,
                actor_id,
                scene,
                store.runtime,
                rag_ids or [],
            )
        )
        checks.extend(self._ability_check(proposal, actor_id, store.runtime))
        checks.extend(
            self._artifact_check(
                proposal, actor_id, scene, store.runtime
            )
        )
        checks.extend(self._rule_check(proposal, actor_id))
        checks.extend(self._time_check(proposal, actor_id, scene, store.runtime))
        checks.extend(self._conflict_check(proposal, store.runtime))

        outcomes = {item["outcome"] for item in checks}
        impact = proposal.get("action_intent", {}).get(
            "impact_level", "dialogue"
        )
        if "blocked" in outcomes:
            status = "blocked"
        elif "needs_resolution" in outcomes:
            status = "needs_resolution"
        elif "uncertain" in outcomes:
            status = "uncertain"
        else:
            status = "allowed"
        commit_allowed = status == "allowed" and (
            impact in NON_STATEFUL_IMPACT_LEVELS
            or bool(
                proposal.get("action_intent", {}).get(
                    "proposed_state_changes"
                )
            )
        )
        return {
            "validation_id": "validation_" + uuid.uuid4().hex[:16],
            "status": status,
            "commit_allowed": commit_allowed,
            "checks": checks,
            "correction_action": {
                "blocked": "discard_effect_keep_user_safe_narrative",
                "needs_resolution": "retrieve_or_disambiguate_then_retry",
                "uncertain": "send_to_gm_adjudication",
                "allowed": "commit_event",
            }[status],
            "user_visible_reason": "",
        }


class SimulationOrchestrator:
    def __init__(
        self,
        world_db,
        character_db,
        agent_profiles,
        store,
        llm_callable,
        max_context_units=12,
        max_nearby_agents=8,
        memory_summary_interval=4,
    ):
        self.world_db = world_db
        self.character_db = character_db
        self.agent_profiles = agent_profiles
        self.store = store
        self.call_llm = llm_callable
        self.max_context_units = max_context_units
        self.max_nearby_agents = max_nearby_agents
        self.memory_summary_interval = memory_summary_interval
        self.validator = WorldValidator(world_db, character_db, agent_profiles)
        self.character_by_id = character_db.get("character_by_id", {})
        self.agent_by_character_id = {
            item["character_id"]: item for item in agent_profiles.get("agents", [])
        }
        self.knowledge_unit_index = self._build_knowledge_unit_index()

    def _runtime_npc_profiles(self):
        return self.store.runtime.setdefault("runtime_npc_profiles", {})

    def _is_runtime_character(self, character_id):
        return clean_text(character_id) in self._runtime_npc_profiles()

    def _is_known_character_id(self, character_id):
        character_id = clean_text(character_id)
        return character_id in self.character_by_id or self._is_runtime_character(character_id)

    def _runtime_npc_id(self, label, location_id):
        label = clean_text(label) or "附近人物"
        location_id = clean_text(location_id) or "unknown_location"
        return "runtime_npc_" + stable_hash(
            {
                "label": label,
                "location_id": location_id,
            }
        )[:16]

    def _matching_runtime_npc_ids(self, label, location_id=""):
        label = clean_text(label)
        location_id = clean_text(location_id)
        if not label:
            return []
        matches = []
        active_scene = self.store.runtime.get("active_scene") or {}
        active_ids = set(active_scene.get("participant_ids", []))
        runtime = self.store.runtime.get("character_runtime", {})
        memories = self.store.runtime.get("agent_memories", {})
        for character_id, profile in self._runtime_npc_profiles().items():
            identity = profile.get("identity", {}) if isinstance(profile, dict) else {}
            names = {
                clean_text(profile.get("canonical_name")),
                clean_text(profile.get("display_name")),
                *[clean_text(item) for item in identity.get("aliases", [])],
                *[
                    clean_text(item)
                    for item in identity.get("canonical_identity_names", [])
                ],
            }
            if label not in names:
                continue
            state = runtime.get(character_id, {})
            state_location = clean_text(state.get("current_location"))
            profile_location = clean_text(
                (profile.get("attributes") or {}).get("location_id")
            )
            same_location = bool(
                location_id
                and (state_location == location_id or profile_location == location_id)
            )
            active_here = character_id in active_ids
            active_state = self._availability_is_active(state.get("availability"))
            memory_revision = bounded_int(
                memories.get(character_id, {}).get("last_revision"),
                default=0,
                minimum=0,
            )
            matches.append(
                (
                    1 if active_here else 0,
                    1 if same_location else 0,
                    1 if active_state else 0,
                    memory_revision,
                    character_id,
                )
            )
        matches.sort(reverse=True)
        return [item[-1] for item in matches]

    def _existing_runtime_npc_id(self, label, location_id=""):
        matches = self._matching_runtime_npc_ids(label, location_id)
        if not matches:
            return ""
        return matches[0]

    def _runtime_npc_profile(
        self,
        character_id,
        label,
        location_id,
        seed_event_id="",
        memory_text="",
    ):
        label = clean_text(label) or "附近人物"
        location_name = self._location_name(location_id)
        return {
            "agent_id": "agent_" + character_id,
            "character_id": character_id,
            "canonical_name": label,
            "profile_tier": "runtime",
            "runtime_mode": "runtime_ambient_promoted_agent",
            "simulation_status": "local_runtime_npc",
            "identity": {
                "canonical_name": label,
                "aliases": [label],
                "titles": [],
                "forms": [],
                "temporary_identities": [],
                "canonical_identity_names": [label],
                "identity_source": "runtime_ambient_npc_promotion",
                "identity_rule": "one_runtime_agent_per_label_and_location",
            },
            "state": {
                "background_summary": (
                    f"{label}是当前局部场景中的普通人物，位于"
                    f"{location_name or location_id}。"
                ),
                "personality": [
                    "普通人，会根据恐惧、利益、习惯和眼前威胁作出反应。"
                ],
                "goals": [
                    "保全自身",
                    "保护自己的住处、同伴或日常生活不被牵连",
                    "回答、回避、拖延或误导强势角色的追问",
                    "一旦出现合理机会，可以逃离、求援、躲藏或改变路线",
                ],
                "constraints": [
                    "没有超出普通人证据范围的知识或能力。",
                    "不是玩家的工具；只会在恐惧、利益或现实压力下暂时配合。",
                ],
                "speech_styles": ["紧张、朴素、符合当地普通人的说话方式。"],
                "knowledge_scope": [
                    "只能知道自己亲历、听闻或本地传言中的事情。",
                    "不知道其他角色内心，也不知道原著未来。",
                ],
            },
            "core_motivation": {
                "true_self": label,
                "true_identity_name": label,
                "root_drives": ["保全自身和日常生活"],
                "current_true_objectives": [
                    "活过当前威胁",
                    "尽量让危险远离自己熟悉的人和地点",
                ],
                "current_objectives": [
                    "活过当前威胁",
                    "尽量让危险远离自己熟悉的人和地点",
                ],
                "fears_or_constraints": [
                    "害怕强势角色迁怒",
                    "害怕说错话招来危险",
                    "只能依据亲历、听闻和本地传言行动",
                ],
                "strategy_identities": [
                    "求饶",
                    "顺从",
                    "指路",
                    "拖延",
                    "误导",
                    "伺机逃离",
                    "求援",
                ],
                "action_policy": {
                    "priority": "保命和保护自身生活优先；恐惧会改变路线，也可以触发逃离、求援、躲藏或误导。",
                    "when_threatened": (
                        "高威胁下可以暂时顺从，但必须继续寻找自保机会；"
                        "如果出现距离、遮蔽物、旁人或混乱等机会，可以逃跑、求援、"
                        "拖延、绕路或给出对自己更安全的信息。"
                    ),
                    "stall_guard": (
                        "不要整轮只反复发抖、磕头或求饶；恐惧之后必须出现"
                        "保命判断、信息取舍或具体行动。"
                    ),
                    "action_bias": "self_preserving_autonomy",
                    "forward_actions": [
                        "观察威胁",
                        "判断退路",
                        "顺从以降低威胁",
                        "拖延",
                        "误导",
                        "逃离",
                        "求援",
                        "躲藏",
                    ],
                    "risk_can_override_goal": True,
                },
            },
            "attributes": {
                "runtime_created": True,
                "source_event_id": clean_text(seed_event_id),
                "location_id": clean_text(location_id),
            },
            "capabilities": {
                "abilities": [],
                "owned_items": [],
                "used_items": [],
            },
            "relationships": [],
            "weak_relation_candidates": [],
            "metadata_relation_candidates": [],
            "world_context": {
                "knowledge_refs": [],
                "supported_retrieval_candidates": [],
            },
            "memories": [
                {
                    "source_text": clean_text(memory_text),
                    "relation_summary": clean_text(memory_text),
                    "source_event_id": clean_text(seed_event_id),
                }
            ] if clean_text(memory_text) else [],
            "evidence_refs": [],
            "guardrails": {
                "runtime_ambient_npc": True,
                "evidence_only": True,
                "unsupported_fields_must_remain_unknown": True,
            },
        }

    def _runtime_npc_profile_with_autonomy(self, profile):
        profile = deep_copy(profile or {})
        if not (profile.get("guardrails") or {}).get("runtime_ambient_npc"):
            return profile
        label = clean_text(profile.get("canonical_name")) or "附近人物"
        state = profile.setdefault("state", {})
        state["personality"] = compact_list(
            [
                *state.get("personality", []),
                "会根据恐惧、利益、习惯和眼前威胁作出反应。",
            ],
            8,
        )
        state["goals"] = compact_list(
            [
                *state.get("goals", []),
                "保全自身",
                "保护自己的住处、同伴或日常生活不被牵连",
                "回答、回避、拖延或误导强势角色的追问",
                "一旦出现合理机会，可以逃离、求援、躲藏或改变路线",
            ],
            12,
        )
        state["constraints"] = compact_list(
            [
                *state.get("constraints", []),
                "没有超出普通人证据范围的知识或能力。",
                "不是玩家的工具；只会在恐惧、利益或现实压力下暂时配合。",
            ],
            10,
        )
        core = profile.setdefault("core_motivation", {})
        core.setdefault("true_self", label)
        core.setdefault("true_identity_name", label)
        core["root_drives"] = compact_list(
            [*core.get("root_drives", []), "保全自身和日常生活"],
            8,
        )
        core["current_true_objectives"] = compact_list(
            [
                *core.get("current_true_objectives", []),
                *core.get("current_objectives", []),
                "活过当前威胁",
                "尽量让危险远离自己熟悉的人和地点",
            ],
            10,
        )
        core["current_objectives"] = deep_copy(core["current_true_objectives"])
        core["fears_or_constraints"] = compact_list(
            [
                *core.get("fears_or_constraints", []),
                *core.get("fears", []),
                "害怕强势角色迁怒",
                "害怕说错话招来危险",
                "只能依据亲历、听闻和本地传言行动",
            ],
            10,
        )
        core["strategy_identities"] = compact_list(
            [
                *core.get("strategy_identities", []),
                *core.get("strategy_patterns", []),
                "求饶",
                "顺从",
                "指路",
                "拖延",
                "误导",
                "伺机逃离",
                "求援",
            ],
            12,
        )
        core["action_policy"] = {
            "priority": "保命和保护自身生活优先；恐惧会改变路线，也可以触发逃离、求援、躲藏、拖延或误导。",
            "when_threatened": (
                "高威胁下可以暂时顺从，但必须继续寻找自保机会；"
                "如果出现距离、遮蔽物、旁人或混乱等机会，可以逃跑、求援、"
                "拖延、绕路或给出对自己更安全的信息。"
            ),
            "stall_guard": (
                "不要整轮只反复发抖、磕头或求饶；恐惧之后必须出现"
                "保命判断、信息取舍或具体行动。"
            ),
            "action_bias": "self_preserving_autonomy",
            "forward_actions": [
                "观察威胁",
                "判断退路",
                "顺从以降低威胁",
                "拖延",
                "误导",
                "逃离",
                "求援",
                "躲藏",
            ],
            "risk_can_override_goal": True,
        }
        return profile

    def _build_knowledge_unit_index(self):
        rows = []
        for unit in self.world_db.get("knowledge_units", []):
            status = unit.get("model_status", "unresolved")
            if status not in {"trusted", "supported"}:
                continue
            haystack = " ".join(
                clean_text(item)
                for item in [
                    unit.get("name", ""),
                    unit.get("category", ""),
                    *unit.get("retrieval_tags", []),
                    *unit.get("descriptions", []),
                ]
                if clean_text(item)
            ).casefold()
            if not haystack:
                continue
            rows.append({
                "unit": unit,
                "haystack": haystack,
                "status": status,
                "trusted": status == "trusted",
            })
        return rows

    @staticmethod
    def _term_score(haystack, terms):
        score = 0
        matched = []
        for term in terms:
            term = clean_text(term).casefold()
            if len(term) < 2:
                continue
            if term in haystack:
                score += 1 + min(len(term), 8) / 8
                matched.append(term)
        return score, compact_list(matched, 16)

    def _search_knowledge_units(self, terms, limit=None):
        scored = []
        for row in self.knowledge_unit_index:
            score, matched = self._term_score(row["haystack"], terms)
            if not score:
                continue
            unit = deep_copy(row["unit"])
            unit["_retrieval_score"] = round(score, 3)
            unit["_matched_terms"] = matched
            scored.append((score, row["trusted"], unit))
        scored.sort(key=lambda item: (-item[0], not item[1], item[2].get("name", "")))
        units = [item[2] for item in scored]
        return units[:limit] if limit else units

    def _resource_is_current_for_character(self, resource_id, character_id, modes):
        if not resource_id:
            return False
        resource_states = self.store.runtime.get("resource_states", {})
        if not resource_states:
            return True
        state = resource_states.get(resource_id)
        if not state:
            return False
        holders = set()
        if "owner" in modes:
            holders |= set(state.get("current_owner_ids", []))
        if "user" in modes:
            holders |= set(state.get("current_user_ids", []))
        if "holder" in modes:
            holders |= set(state.get("current_holder_ids", []))
        return character_id in holders

    @staticmethod
    def _prioritized_relationship_context(relationships, limit=24):
        specific_by_other = {
            (
                item.get("entity_id")
                or "|".join(item.get("participant_ids", []))
                or item.get("name", "")
            )
            for item in relationships
            if clean_text(
                item.get("relation_type")
                or item.get("current_value")
                or ",".join(item.get("current_labels", []))
            ).upper()
            not in {"", "HAS_RELATIONSHIP", "CO_OCCURS_IN_SCENE"}
        }
        priority = {
            "PARENT_OF": 100,
            "CHILD_OF": 100,
            "DISCIPLE_OF": 95,
            "MASTER_OF": 95,
            "PROTECTS": 90,
            "TRAVELS_WITH": 86,
            "FIGHTS_WITH": 84,
            "ENEMY_OF": 84,
            "OWNS_ARTIFACT": 70,
            "USES_ARTIFACT": 66,
            "USES_ABILITY": 66,
            "HAS_RELATIONSHIP": 10,
        }
        filtered = []
        for item in relationships:
            relation_type = clean_text(
                item.get("relation_type")
                or item.get("current_value")
                or ",".join(item.get("current_labels", []))
            ).upper()
            other_key = (
                item.get("entity_id")
                or "|".join(item.get("participant_ids", []))
                or item.get("name", "")
            )
            if (
                relation_type in {"HAS_RELATIONSHIP", "CO_OCCURS_IN_SCENE"}
                and other_key in specific_by_other
            ):
                continue
            filtered.append(item)
        filtered.sort(
            key=lambda item: (
                -priority.get(
                    clean_text(
                        item.get("relation_type")
                        or item.get("current_value")
                        or ",".join(item.get("current_labels", []))
                    ).upper(),
                    50,
                ),
                item.get("confidence") == "low",
                item.get("name", ""),
            )
        )
        return compact_list(filtered, limit)

    def _profile_with_current_capabilities(self, profile):
        profile = deep_copy(profile)
        character_id = profile["character_id"]
        capabilities = profile.setdefault("capabilities", {})
        capabilities["abilities"] = [
            item
            for item in capabilities.get("abilities", [])
            if self._resource_is_current_for_character(
                item.get("entity_id"), character_id, {"owner", "user"}
            )
        ]
        capabilities["owned_items"] = [
            item
            for item in capabilities.get("owned_items", [])
            if self._resource_is_current_for_character(
                item.get("entity_id"), character_id, {"owner", "holder"}
            )
        ]
        capabilities["used_items"] = [
            item
            for item in capabilities.get("used_items", [])
            if self._resource_is_current_for_character(
                item.get("entity_id"), character_id, {"owner", "holder", "user"}
            )
        ]
        runtime_relationships = []
        for relation in self.store.runtime.get("relationship_states", {}).values():
            participant_ids = relation.get("participant_ids", [])
            if character_id not in participant_ids:
                continue
            runtime_relationships.append(
                {
                    "relationship_id": relation.get("relationship_id"),
                    "participant_ids": participant_ids,
                    "participant_names": relation.get("participant_names", []),
                    "current_value": relation.get("current_value"),
                    "status": relation.get("status"),
                    "source": relation.get("source", "runtime"),
                }
            )
        if runtime_relationships:
            profile["runtime_relationships"] = runtime_relationships[:20]
            profile["relationships"] = compact_list(
                profile.get("relationships", []) + runtime_relationships,
                24,
            )
        profile["relationships"] = self._prioritized_relationship_context(
            profile.get("relationships", []),
            24,
        )
        profile["core_motivation"] = self._infer_core_motivation(profile)
        profile = self._profile_with_admin_overrides(profile)
        return profile

    def _profile_with_admin_overrides(self, profile):
        character_id = profile.get("character_id")
        overrides = (
            self.store.runtime.get("admin_profile_overrides", {})
            .get(character_id, {})
        )
        if not isinstance(overrides, dict) or not overrides:
            return profile
        profile = deep_copy(profile)
        profile["admin_profile_overrides"] = deep_copy(overrides)
        state_override = overrides.get("state", {})
        if isinstance(state_override, dict):
            state = profile.setdefault("state", {})
            for key, value in state_override.items():
                if value not in (None, "", [], {}):
                    state[key] = deep_copy(value)
        for key in ("personality", "goals", "constraints", "speech_styles", "knowledge_scope"):
            if key in overrides and overrides[key] not in (None, "", [], {}):
                profile.setdefault("state", {})[key] = deep_copy(overrides[key])
        core_override = overrides.get("core_motivation", {})
        if isinstance(core_override, dict) and core_override:
            core = deep_copy(profile.get("core_motivation", {}))
            for key, value in core_override.items():
                if value not in (None, "", [], {}):
                    core[key] = deep_copy(value)
            core["action_policy"] = self._motivation_action_policy(core)
            profile["core_motivation"] = core
        identity_override = overrides.get("identity", {})
        if isinstance(identity_override, dict):
            identity = profile.setdefault("identity", {})
            for key, value in identity_override.items():
                if value not in (None, "", [], {}):
                    identity[key] = deep_copy(value)
        note = clean_text(overrides.get("admin_note"))
        if note:
            profile["admin_note"] = note
        return profile

    def _infer_core_motivation(self, profile):
        character_id = profile.get("character_id")
        character = self.character_by_id.get(character_id, {})
        novel_track = (
            self.world_db.get("canonical_novel_db", {})
            .get("entity_tracks", {})
            .get(character_id, {})
        )
        existing = deep_copy(
            profile.get("core_motivation")
            or character.get("core_motivation")
            or novel_track.get("core_motivation")
            or {}
        )
        if existing and (
            existing.get("root_drives")
            or existing.get("current_objectives")
            or existing.get("current_true_objectives")
        ):
            existing.setdefault(
                "true_self",
                existing.get("true_identity_name")
                or profile.get("canonical_name")
                or character.get("canonical_name", ""),
            )
            existing.setdefault(
                "true_identity_name",
                profile.get("canonical_name")
                or character.get("canonical_name", ""),
            )
            if "current_true_objectives" not in existing:
                existing["current_true_objectives"] = deep_copy(
                    existing.get("current_objectives", [])
                )
            if "current_objectives" not in existing:
                existing["current_objectives"] = deep_copy(
                    existing.get("current_true_objectives", [])
                )
            if "fears_or_constraints" not in existing:
                existing["fears_or_constraints"] = deep_copy(
                    existing.get("fears", [])
                )
            if "strategy_identities" not in existing:
                strategy = deep_copy(existing.get("strategy_patterns", []))
                for item in (
                    profile.get("identity_layers", {})
                    or character.get("identity_layers", {})
                    or {}
                ).get("roleplay_identities", []):
                    if isinstance(item, dict):
                        strategy.append(
                            clean_text(item.get("purpose"))
                            or clean_text(item.get("identity"))
                        )
                existing["strategy_identities"] = compact_list(
                    [item for item in strategy if clean_text(item)],
                    12,
                )
            existing.setdefault(
                "roleplay_identity_policy",
                existing.get("identity_policy")
                or (
                    profile.get("identity_layers", {})
                    or character.get("identity_layers", {})
                    or {}
                ).get("policy", ""),
            )
            existing.setdefault(
                "action_policy",
                self._motivation_action_policy(existing),
            )
            existing.setdefault("source_basis", [])
            existing.setdefault("support_notes", existing.get("inference_notes", []))
            return existing
        identity = profile.get("identity", {})
        state = profile.get("state", {})
        attributes = {
            **deep_copy(novel_track.get("attributes", {}) or {}),
            **deep_copy(character.get("attributes", {}) or {}),
            **deep_copy(profile.get("attributes", {}) or {}),
        }
        background_evidence = compact_list(
            [
                *state.get("background_evidence", []),
                *character.get("background", []),
                *novel_track.get("descriptions", []),
            ],
            12,
        )
        evidence_texts = []
        for item in profile.get("evidence_refs", []):
            evidence_texts.append(item.get("source_text"))
        for item in character.get("evidence_refs", []):
            evidence_texts.append(item.get("source_text"))
        for item in novel_track.get("evidence_refs", []):
            evidence_texts.append(item.get("source_text"))
        for item in profile.get("source_evidence_refs", []):
            evidence_texts.extend(item.get("snippets", []))
        relation_texts = []
        for item in profile.get("relationships", []):
            relation_texts.append(item.get("edge_statement"))
            for evidence in item.get("evidence", [])[:2]:
                relation_texts.append(evidence.get("source_text"))
        source_basis = [
            clean_text(item)
            for item in compact_list(
                [
                    state.get("background_summary"),
                    *background_evidence,
                    *state.get("goals", []),
                    *state.get("constraints", []),
                    *state.get("personality", []),
                    *state.get("knowledge_scope", []),
                    *evidence_texts,
                    *relation_texts,
                ],
                40,
            )
            if clean_text(item)
        ]
        basis_text = "；".join(source_basis)
        lower_basis = basis_text.casefold()
        canonical_name = profile.get("canonical_name", "")
        subtypes = compact_list(
            attributes.get("entity_subtype", [])
            if isinstance(attributes.get("entity_subtype"), list)
            else [attributes.get("entity_subtype", "")],
            8,
        )
        true_self_parts = [canonical_name]
        if subtypes:
            true_self_parts.append("、".join(subtypes))
        elif "妖怪" in basis_text or canonical_name.endswith("精"):
            true_self_parts.append("妖怪")
        true_self = "（".join(true_self_parts[:2])
        if len(true_self_parts) > 1:
            true_self += "）"

        root_drives = compact_list(state.get("goals", []), 8)
        current_objectives = compact_list(state.get("goals", []), 8)
        fears_or_constraints = compact_list(state.get("constraints", []), 8)
        strategies = []
        support_notes = []

        if "骗取唐僧肉" in basis_text or (
            "唐僧肉" in basis_text and ("骗" in basis_text or "变身" in basis_text)
        ):
            current_objectives.append("通过变身接近唐僧并骗取唐僧肉")
            root_drives.append("获取唐僧肉以延续生命、增长修为或追求长生")
            strategies.append("把外在身份当作接近唐僧的临时伪装")
            support_notes.append("来源写明通过变身骗取唐僧肉；更深层欲望按妖怪行动逻辑推断")
        elif "唐僧肉" in basis_text:
            current_objectives.append("围绕唐僧肉寻找机会")
            root_drives.append("获取唐僧肉带来的生存或修为利益")
            support_notes.append("来源提到唐僧肉；具体欲望按行动目标推断")

        if "变身" in basis_text or identity.get("forms") or identity.get("temporary_identities"):
            strategies.append("伪装、变身或临时身份是行动策略，不是本体人格")
        if "妖怪" in basis_text or "demon" in lower_basis or canonical_name.endswith("精"):
            root_drives.append("保全妖怪本体，避开能识破或伤害自己的对手")
        if "火眼金睛" in basis_text and ("识破妖怪" in basis_text or "识破" in basis_text):
            fears_or_constraints.append("孙悟空的火眼金睛会带来暴露风险")
        if "紧箍咒" in basis_text and "孙悟空" in basis_text:
            fears_or_constraints.append("唐僧能用紧箍咒牵制孙悟空，这可能改变风险判断")
        if "妖怪" in basis_text or "demon" in lower_basis or canonical_name.endswith("精"):
            fears_or_constraints.append("本体暴露后遭到降伏或诛杀")

        root_drives = compact_list(root_drives, 8)
        current_objectives = compact_list(current_objectives, 8)
        strategies = compact_list(strategies, 8)
        fears_or_constraints = compact_list(fears_or_constraints, 8)
        support_notes = compact_list(support_notes, 6)
        if not root_drives:
            root_drives = ["维持本体生存，并按既有身份、关系和处境追求利益"]
            support_notes.append("资料未给出显式长期欲望，运行时只做保守动机占位")
        if not current_objectives:
            current_objectives = ["延续当前处境中最符合本体利益的目标"]
        return {
            "true_self": true_self,
            "true_identity_name": canonical_name,
            "root_drives": root_drives,
            "current_true_objectives": current_objectives,
            "fears_or_constraints": fears_or_constraints,
            "strategy_identities": strategies,
            "roleplay_identity_policy": (
                "本体、欲望和长期目标优先；任何村姑、老人、商旅、仆从、"
                "化身或日常身份都只是角色为了达成目的而表演的外层身份。"
                "叙事可以写伪装的动作，但私下判断必须来自真实本体。"
            ),
            "action_policy": self._motivation_action_policy(
                {
                    "root_drives": root_drives,
                    "current_true_objectives": current_objectives,
                    "fears_or_constraints": fears_or_constraints,
                    "strategy_identities": strategies,
                    "temptations": compact_list(
                        ["唐僧肉"] if "唐僧肉" in basis_text else [],
                        4,
                    ),
                }
            ),
            "source_basis": source_basis[:12],
            "support_notes": support_notes,
        }

    def current_canonical_event(self):
        cursor = int(self.store.runtime.get("timeline_cursor", 0) or 0)
        timeline = self._timeline_nodes()
        if not timeline:
            return {}
        return deep_copy(timeline[min(cursor, len(timeline) - 1)])

    def _current_trigger_analysis(self, profile, core_motivation):
        anchor = self.current_canonical_event()
        anchor_text = "；".join(
            clean_text(item)
            for item in compact_list(
                [
                    anchor.get("event"),
                    anchor.get("default_outcome"),
                    *anchor.get("participant_names", []),
                    *[
                        item.get("name")
                        for item in anchor.get("ability_refs", [])
                    ],
                    *[
                        item.get("name")
                        for item in anchor.get("artifact_refs", [])
                    ],
                ],
                30,
            )
            if clean_text(item)
        )
        objectives = "；".join(core_motivation.get("current_true_objectives", []))
        triggers = []
        if "唐僧" in anchor_text and ("唐僧" in objectives or "唐僧肉" in objectives):
            triggers.append("看见或确认唐僧会触发接近、诱骗或下手的机会判断")
        if "孙悟空" in anchor_text or "悟空" in anchor_text:
            triggers.append("孙悟空在场会触发暴露、被识破和正面冲突风险判断")
        if "火眼金睛" in anchor_text or "识破妖怪" in anchor_text:
            triggers.append("火眼金睛相关信息会让伪装策略变得紧迫且危险")
        if (
            any(word in anchor_text for word in ("唐僧", "唐僧肉"))
            and any(word in anchor_text for word in ("孙悟空", "悟空", "火眼金睛"))
            and ("唐僧" in objectives or "唐僧肉" in objectives)
        ):
            triggers.append(
                "欲望与风险同时存在：恐惧只能迫使其换身份、绕开视线、试探或诱导，不能取消接近唐僧的核心目标"
            )
        if not triggers:
            triggers.append("按本体欲望、当前目标和可见风险决定下一步")
        return {
            "current_anchor_text": anchor_text,
            "triggered_desires_and_judgments": compact_list(triggers, 6),
        }

    @staticmethod
    def _core_current_objectives(core_motivation):
        return (
            core_motivation.get("current_true_objectives")
            or core_motivation.get("current_objectives")
            or []
        )

    @staticmethod
    def _core_fears(core_motivation):
        return (
            core_motivation.get("fears_or_constraints")
            or core_motivation.get("fears")
            or []
        )

    @staticmethod
    def _core_strategies(core_motivation):
        return (
            core_motivation.get("strategy_identities")
            or core_motivation.get("strategy_patterns")
            or []
        )

    def _motivation_action_policy(self, source):
        source = source or {}
        objectives = (
            source.get("current_objectives")
            or self._core_current_objectives(source)
        )
        fears = source.get("fears") or self._core_fears(source)
        strategies = source.get("strategies") or self._core_strategies(source)
        text = "；".join(
            clean_text(item)
            for item in [
                *source.get("root_drives", []),
                *objectives,
                *fears,
                *strategies,
                *source.get("temptations", []),
                *source.get("trigger_rules", []),
            ]
            if clean_text(item)
        )
        pursues_tang = "唐僧" in text or "唐僧肉" in text
        has_high_threat = any(
            word in text
            for word in ("孙悟空", "悟空", "火眼金睛", "识破", "暴露", "降伏", "诛杀")
        )
        self_preserving = any(
            word in text
            for word in (
                "保全自身",
                "保命",
                "活过",
                "逃离",
                "求援",
                "躲藏",
                "误导",
                "拖延",
                "危险远离",
            )
        )
        uses_disguise = any(
            word in text
            for word in ("伪装", "变身", "化身", "临时身份", "骗", "诱")
        )
        forward_actions = ["观察", "试探", "换策略", "保留下一步机会"]
        if uses_disguise:
            forward_actions.extend(["维护伪装", "变换身份", "制造误判"])
        if pursues_tang:
            forward_actions.extend(["接近唐僧", "诱导唐僧", "分散护卫注意"])
        if has_high_threat:
            forward_actions.extend(["绕开孙悟空视线", "拉开风险距离", "等待护卫破绽"])
        if pursues_tang and has_high_threat:
            action_bias = "risk_managed_pursuit"
        elif self_preserving:
            action_bias = "self_preserving_autonomy"
            forward_actions.extend(["寻找退路", "拖延", "误导", "逃离", "求援", "躲藏"])
        else:
            action_bias = "goal_directed_survival"
        return {
            "priority": (
                "根本欲望决定方向；若根本欲望是保命，恐惧本身就是行动方向，"
                "可以触发逃离、求援、躲藏、拖延或误导。"
            ),
            "when_threatened": (
                "高威胁下先隐蔽推进核心目标：维护伪装、换身份、转移视线、"
                "分散威胁、试探目标或制造机会；若角色目标是自保，暂避、逃跑、"
                "求援或给出不完全信息都可以是目标导向行动。"
            ),
            "stall_guard": (
                "除非玩家明确要求或角色被外力限制，不要整轮只屏息、僵住、"
                "装死、扮石头或反复描写恐惧；停顿之后必须出现目标导向的判断或动作。"
            ),
            "action_bias": action_bias,
            "forward_actions": compact_list(forward_actions, 12),
            "risk_can_override_goal": bool(self_preserving and not pursues_tang),
        }

    def _motivation_terms(self, profile):
        core = profile.get("core_motivation", {}) or {}
        terms = [
            profile.get("canonical_name", ""),
            core.get("true_self", ""),
            *core.get("root_drives", []),
            *self._core_current_objectives(core),
            *self._core_fears(core),
            *core.get("attachments", []),
            *core.get("temptations", []),
            *self._core_strategies(core),
            *core.get("trigger_rules", []),
            *core.get("emotional_baseline", []),
        ]
        for item in profile.get("identity_layers", {}).get(
            "roleplay_identities", []
        ):
            if isinstance(item, dict):
                terms.extend([item.get("identity", ""), item.get("purpose", "")])
        result = []
        for text in terms:
            text = clean_text(text)
            if not text:
                continue
            result.append(text.casefold())
            for sequence in re.findall("[\u4e00-\u9fff]{4,}", text):
                for size in (2, 3, 4):
                    for index in range(0, max(0, len(sequence) - size + 1)):
                        result.append(sequence[index:index + size])
        return compact_list(result, 48)

    def _root_missing_fields(self, core_motivation):
        missing = []
        if not clean_text(core_motivation.get("true_self")):
            missing.append("true_self")
        if not core_motivation.get("root_drives"):
            missing.append("root_drives")
        if not self._core_current_objectives(core_motivation):
            missing.append("current_objectives")
        if not self._core_fears(core_motivation):
            missing.append("fears")
        if not self._core_strategies(core_motivation):
            missing.append("strategy_patterns")
        if not core_motivation.get("trigger_rules") and not core_motivation.get(
            "current_trigger_analysis"
        ):
            missing.append("trigger_rules")
        if not core_motivation.get("source_basis"):
            missing.append("source_basis")
        return missing

    def _character_root_lookup(self, profile):
        core = deep_copy(profile.get("core_motivation", {}) or {})
        if core:
            core.setdefault(
                "current_trigger_analysis",
                self._current_trigger_analysis(profile, core),
            )
            trigger_text = clean_text(
                "；".join(
                    core.get("current_trigger_analysis", {}).get(
                        "triggered_desires_and_judgments",
                        [],
                    )
                )
            )
            if (
                any(word in trigger_text for word in ("孙悟空", "悟空", "火眼金睛", "识破"))
                and not any(
                    "孙悟空" in clean_text(item) or "火眼金睛" in clean_text(item)
                    for item in self._core_fears(core)
                )
            ):
                core["fears_or_constraints"] = compact_list(
                    [
                        *self._core_fears(core),
                        "孙悟空或火眼金睛会带来暴露和正面冲突风险",
                    ],
                    8,
                )
        identity_layers = deep_copy(profile.get("identity_layers", {}) or {})
        missing = self._root_missing_fields(core)
        if not missing:
            coverage = "good"
        elif len(missing) <= 2:
            coverage = "partial"
        else:
            coverage = "thin"
        return {
            "tool": "character_root_lookup",
            "coverage": coverage,
            "missing_fields": missing,
            "true_self": core.get("true_self")
                or profile.get("canonical_name", ""),
            "root_drives": deep_copy(core.get("root_drives", []))[:8],
            "current_objectives": deep_copy(
                self._core_current_objectives(core)
            )[:8],
            "fears": deep_copy(self._core_fears(core))[:8],
            "attachments": deep_copy(core.get("attachments", []))[:8],
            "temptations": deep_copy(core.get("temptations", []))[:8],
            "strategies": deep_copy(self._core_strategies(core))[:8],
            "trigger_rules": deep_copy(core.get("trigger_rules", []))[:8],
            "action_policy": deep_copy(
                core.get("action_policy")
                or self._motivation_action_policy(core)
            ),
            "current_trigger_analysis": deep_copy(
                core.get("current_trigger_analysis", {})
            ),
            "identity_layers": identity_layers,
            "source_basis": deep_copy(core.get("source_basis", []))[:10],
            "inference_notes": deep_copy(
                core.get("inference_notes")
                or core.get("support_notes", [])
            )[:6],
        }

    def _graph_neighborhood_tool(self, profile):
        threats = []
        attachments = []
        opportunities = []
        relation_rows = []
        threat_types = {
            "FIGHTS_WITH", "HAS_CONFLICT_WITH", "OPPOSES", "ENEMY_OF",
            "OFFENDS", "ORDERS_CAPTURE_OF",
        }
        attachment_types = {
            "PROTECTS", "PARENT_OF", "CHILD_OF", "MASTER_OF",
            "DISCIPLE_OF", "TRAVELS_WITH", "ACCOMPANIES",
            "COMPANION_OF", "SWORN_SIBLING_OF",
        }
        for relation in profile.get("relationships", [])[:24]:
            relation_type = clean_text(
                relation.get("relation_type")
                or relation.get("current_value")
                or ",".join(relation.get("current_labels", []))
            ).upper()
            name = clean_text(
                relation.get("name")
                or ",".join(relation.get("participant_names", []))
            )
            row = {
                "relation_type": relation_type,
                "name": name,
                "confidence": relation.get("confidence")
                    or relation.get("status", ""),
                "entity_id": relation.get("entity_id"),
                "evidence": [
                    clean_text(item.get("source_text"))
                    for item in relation.get("evidence", [])[:2]
                    if clean_text(item.get("source_text"))
                ],
            }
            relation_rows.append(row)
            if relation_type in threat_types:
                threats.append(row)
            elif relation_type in attachment_types:
                attachments.append(row)
            elif relation_type in {"USES_ABILITY", "OWNS_ARTIFACT", "USES_ARTIFACT"}:
                opportunities.append(row)
        capabilities = []
        for group_name in ("abilities", "owned_items", "used_items"):
            for item in profile.get("capabilities", {}).get(group_name, []):
                capabilities.append({
                    "kind": group_name,
                    "entity_id": item.get("entity_id"),
                    "name": item.get("name")
                        or item.get("canonical_name")
                        or item.get("surface_name"),
                })
        scene = self.store.runtime.get("active_scene") or {}
        nearby = [
            {
                "character_id": character_id,
                "name": self.character_by_id.get(character_id, {}).get(
                    "canonical_name", character_id
                ),
            }
            for character_id in scene.get("participant_ids", [])
            if character_id != profile.get("character_id")
        ]
        return {
            "tool": "graph_neighborhood_tool",
            "relationships": relation_rows[:12],
            "threats": threats[:6],
            "attachments": attachments[:6],
            "opportunities": opportunities[:6],
            "capabilities": capabilities[:12],
            "nearby_characters": nearby[:12],
        }

    def _motivation_evidence_retriever(
        self,
        profile,
        terms,
        global_retrieval,
        limit=10,
    ):
        root_terms = self._motivation_terms(profile)
        effective_terms = compact_list(
            [*terms, *root_terms],
            80,
        )
        candidates = []
        seen = set()

        def add(score, record):
            text = clean_text(record.get("source_text"))
            if not text:
                return
            marker = (
                record.get("source"),
                record.get("source_chunk_id"),
                text,
                record.get("timeline_id"),
            )
            if marker in seen:
                return
            seen.add(marker)
            row = deep_copy(record)
            row["source_text"] = text
            row["score"] = round(score, 3)
            candidates.append(row)

        for text in [
            *profile.get("core_motivation", {}).get("source_basis", []),
            *profile.get("state", {}).get("background_evidence", []),
        ]:
            text = clean_text(text)
            if not text:
                continue
            haystack = text.casefold()
            score, matched = self._term_score(haystack, effective_terms)
            add(
                score + 6,
                {
                    "source": "core_motivation_source_basis",
                    "source_chunk_id": "",
                    "source_text": text,
                    "tags": [
                        profile.get("canonical_name", ""),
                        "core_motivation",
                    ],
                    "character_id": profile.get("character_id"),
                    "character_name": profile.get("canonical_name"),
                    "matched_terms": matched,
                },
            )

        for record in global_retrieval.get("source_snippets", []):
            if record.get("character_id") not in {
                profile.get("character_id"), "", None,
            }:
                continue
            haystack = " ".join([
                record.get("source_text", ""),
                *record.get("tags", []),
            ]).casefold()
            score, matched = self._term_score(haystack, effective_terms)
            if score:
                row = deep_copy(record)
                row["matched_terms"] = compact_list(
                    [*row.get("matched_terms", []), *matched],
                    16,
                )
                add(score + row.get("weight", 1), row)

        for record in self._profile_source_snippets(profile):
            haystack = " ".join([
                record.get("source_text", ""),
                record.get("character_name", ""),
                *record.get("tags", []),
            ]).casefold()
            score, matched = self._term_score(haystack, effective_terms)
            if not score and record.get("source") not in {
                "relationship_evidence", "event_ref",
            }:
                continue
            row = deep_copy(record)
            row["matched_terms"] = matched
            add(score + row.get("weight", 1), row)

        for unit in self._search_knowledge_units(effective_terms, limit=12):
            text = "；".join(
                clean_text(item)
                for item in [
                    unit.get("name", ""),
                    *unit.get("descriptions", [])[:3],
                ]
                if clean_text(item)
            )
            if not text:
                continue
            add(
                float(unit.get("_retrieval_score", 1)),
                {
                    "source": "knowledge_unit",
                    "source_chunk_id": ",".join(
                        str(item) for item in unit.get("source_chunk_ids", [])
                    ),
                    "source_text": text,
                    "tags": unit.get("retrieval_tags", []),
                    "character_id": "",
                    "character_name": "",
                    "matched_terms": unit.get("_matched_terms", []),
                    "knowledge_id": unit.get("knowledge_id"),
                    "entity_id": unit.get("entity_id"),
                    "model_status": unit.get("model_status"),
                },
            )

        anchor = self.current_canonical_event()
        anchor_text = clean_text(anchor.get("default_outcome"))
        if anchor_text:
            haystack = " ".join([
                anchor_text,
                anchor.get("event", ""),
                *anchor.get("participant_names", []),
            ]).casefold()
            score, matched = self._term_score(haystack, effective_terms)
            participant_overlap = profile.get("character_id") in anchor.get(
                "participants", []
            )
            if score or participant_overlap:
                add(
                    score + (4 if participant_overlap else 0),
                    {
                        "source": "current_canonical_anchor",
                        "timeline_id": anchor.get("timeline_id"),
                        "source_chunk_id": str(
                            (anchor.get("source_chunk_ids") or [""])[0]
                        ),
                        "source_text": anchor_text,
                        "tags": [
                            anchor.get("event", ""),
                            *anchor.get("participant_names", []),
                        ],
                        "character_id": "",
                        "character_name": "",
                        "matched_terms": matched,
                    },
                )

        candidates.sort(key=lambda item: (-item.get("score", 0), item.get("source", "")))
        snippets = candidates[:limit]
        return {
            "tool": "motivation_evidence_retriever",
            "query_terms": sorted(effective_terms)[:48],
            "root_terms": root_terms[:32],
            "evidence_snippets": snippets,
            "evidence_count": len(snippets),
            "policy": (
                "这些证据用于补足角色本体、欲望、恐惧、策略和当前触发判断；"
                "证据不足时只能保守推断并标注不确定。"
            ),
        }

    def _retrieval_quality_gate(self, root_lookup, motivation_evidence):
        missing = root_lookup.get("missing_fields", [])
        evidence_count = int(motivation_evidence.get("evidence_count", 0) or 0)
        if not missing and evidence_count >= 2:
            status = "strong"
        elif len(missing) <= 2 and evidence_count >= 1:
            status = "usable"
        else:
            status = "thin"
        actions = []
        if "true_self" in missing or "root_drives" in missing:
            actions.append("扩大角色原文证据和关系邻域检索")
        if "current_objectives" in missing or "trigger_rules" in missing:
            actions.append("检查当前 timeline anchor 与角色目标是否相交")
        if evidence_count == 0:
            actions.append("不要让模型自由补设定，只能按保守生存/利益动机行动")
        if not actions:
            actions.append("可直接用于本轮 agent 决策")
        return {
            "tool": "retrieval_quality_gate",
            "status": status,
            "missing_fields": missing,
            "evidence_count": evidence_count,
            "recommended_actions": actions,
            "decision_policy": (
                "strong/usable 可驱动行动；thin 时必须降低自信，优先观察、试探、"
                "保守行动或请求更多证据。"
            ),
        }

    @staticmethod
    def _intensity_shift(current, delta):
        return bounded_int(
            int(current or 0) + int(delta or 0),
            default=0,
            minimum=0,
            maximum=100,
        )

    def _baseline_motivation_runtime(self, profile, root_lookup=None):
        root_lookup = root_lookup or self._character_root_lookup(profile)
        root_text = "；".join(
            clean_text(item)
            for item in [
                *root_lookup.get("root_drives", []),
                *root_lookup.get("current_objectives", []),
                *root_lookup.get("fears", []),
                *root_lookup.get("strategies", []),
                *root_lookup.get("temptations", []),
            ]
            if clean_text(item)
        )
        pursues_tang = "唐僧" in root_text or "唐僧肉" in root_text
        has_fears = bool(root_lookup.get("fears"))
        has_disguise = any(
            "伪装" in clean_text(item) or "变身" in clean_text(item)
            for item in root_lookup.get("strategies", [])
        )
        action_policy = root_lookup.get("action_policy") or self._motivation_action_policy(
            root_lookup
        )
        return {
            "character_id": profile.get("character_id"),
            "dominant_drive": clean_text(
                (root_lookup.get("root_drives") or [""])[0]
            ),
            "active_objective": clean_text(
                (root_lookup.get("current_objectives") or [""])[0]
            ),
            "active_fear": clean_text(
                (root_lookup.get("fears") or [""])[0]
            ),
            "current_strategy": clean_text(
                (root_lookup.get("strategies") or [""])[0]
            ),
            "desire_intensity": (
                55 if pursues_tang else 35 if root_lookup.get("root_drives") else 10
            ),
            "fear_intensity": (
                35 if pursues_tang and has_fears else 25 if has_fears else 5
            ),
            "attachment_focus": clean_text(
                (root_lookup.get("attachments") or [""])[0]
            ),
            "temptation_focus": clean_text(
                (root_lookup.get("temptations") or [""])[0]
            ),
            "disguise_pressure": (
                55 if pursues_tang and has_disguise else 35 if has_disguise
                else 0
            ),
            "action_policy": deep_copy(action_policy),
            "confidence": root_lookup.get("coverage", "unknown"),
            "last_trigger": "",
            "last_updated_by_event_id": None,
            "history": [],
        }

    def _motivation_delta_for_actor(
        self,
        profile,
        actor_action,
        resolution,
        actor_packet=None,
    ):
        actor_packet = actor_packet or {}
        root_lookup = (
            actor_packet.get("internal_tools", {}).get("character_root_lookup")
            or self._character_root_lookup(profile)
        )
        quality = actor_packet.get("internal_tools", {}).get(
            "retrieval_quality_gate", {}
        )
        current = self.store.runtime.get("motivation_runtime", {}).get(
            profile["character_id"],
            {},
        )
        if not current:
            current = self._baseline_motivation_runtime(profile, root_lookup)
        text = clean_text(
            "；".join(
                [
                    actor_action.get("resolved_intent", ""),
                    actor_action.get("visible_behavior", ""),
                    actor_action.get("goal", ""),
                    actor_action.get("emotion", ""),
                    actor_action.get("dialogue", ""),
                    actor_action.get("action_intent", {}).get("description", ""),
                    resolution.get("outcome", ""),
                    resolution.get("divergence_reason", ""),
                    *[
                        clean_text(item)
                        for item in resolution.get("consequences", [])
                    ],
                ]
            )
        )
        desire_delta = 0
        fear_delta = 0
        disguise_delta = 0
        trigger_notes = []

        for objective in root_lookup.get("current_objectives", []):
            objective = clean_text(objective)
            if objective and any(term in text for term in re.findall("[\u4e00-\u9fff]{2,4}", objective)):
                desire_delta += 8
                trigger_notes.append("本轮行动触及当前目的")
                break
        for drive in root_lookup.get("root_drives", []):
            drive = clean_text(drive)
            if drive and any(term in text for term in re.findall("[\u4e00-\u9fff]{2,4}", drive)):
                desire_delta += 5
                break
        for fear in root_lookup.get("fears", []):
            fear = clean_text(fear)
            if fear and any(term in text for term in re.findall("[\u4e00-\u9fff]{2,4}", fear)):
                fear_delta += 10
                trigger_notes.append("本轮行动触及恐惧")
                break
        if any(word in text for word in ("暴露", "识破", "火眼金睛", "危险", "冲突")):
            fear_delta += 12
            disguise_delta += 8
            trigger_notes.append("暴露或冲突风险上升")
        if any(word in text for word in ("伪装", "变身", "骗", "隐瞒", "试探")):
            disguise_delta += 10
            trigger_notes.append("伪装策略被激活")
        if any(word in text for word in ("成功", "接近", "得手", "机会")):
            desire_delta += 8
        if any(word in text for word in ("失败", "阻止", "受伤", "惩罚", "逃")):
            fear_delta += 8
            desire_delta -= 3
        if resolution.get("outcome") == "success":
            desire_delta += 4
        elif resolution.get("outcome") in {"failed", "partial"}:
            fear_delta += 4
        if quality.get("status") == "thin":
            fear_delta += 3
            trigger_notes.append("证据质量薄，行动自信降低")

        trigger_analysis = root_lookup.get("current_trigger_analysis", {})
        trigger_text = clean_text(
            "；".join(trigger_analysis.get("triggered_desires_and_judgments", []))
        )
        if trigger_text:
            trigger_notes.append(trigger_text)
            if any(word in trigger_text for word in ("接近", "诱骗", "捕获", "下手", "机会")):
                desire_delta += 10
            if any(word in trigger_text for word in ("伪装", "换身份", "绕开", "试探", "诱导")):
                disguise_delta += 8
            if any(word in trigger_text for word in ("风险", "暴露", "识破", "火眼金睛")):
                fear_delta += 6

        action_policy = root_lookup.get("action_policy") or self._motivation_action_policy(
            root_lookup
        )
        if action_policy.get("action_bias") == "risk_managed_pursuit":
            desire_delta += 4
            disguise_delta += 4

        dominant_drive = clean_text(
            current.get("dominant_drive")
            or (root_lookup.get("root_drives") or [""])[0]
        )
        active_objective = clean_text(
            actor_action.get("goal")
            or current.get("active_objective")
            or (root_lookup.get("current_objectives") or [""])[0]
        )
        active_fear = clean_text(
            current.get("active_fear")
            or (root_lookup.get("fears") or [""])[0]
        )
        current_strategy = clean_text(
            current.get("current_strategy")
            or (root_lookup.get("strategies") or [""])[0]
        )
        if any(word in text for word in ("伪装", "变身", "骗", "试探")):
            strategy_candidates = [
                item for item in root_lookup.get("strategies", [])
                if any(word in clean_text(item) for word in ("伪装", "变身", "骗", "试探"))
            ]
            current_strategy = clean_text(
                (strategy_candidates or [current_strategy])[0]
            )
        if (
            action_policy.get("action_bias") == "risk_managed_pursuit"
            and not current_strategy
        ):
            current_strategy = "在高风险下隐蔽推进核心目标"
        next_desire_intensity = self._intensity_shift(
            current.get("desire_intensity"), desire_delta
        )
        next_fear_intensity = self._intensity_shift(
            current.get("fear_intensity"), fear_delta
        )
        if action_policy.get("action_bias") == "risk_managed_pursuit":
            next_desire_intensity = max(next_desire_intensity, 50)
        history_entry = {
            "revision": self.store.branch.get("head_revision", 0) + 1,
            "event_id": "",
            "desire_delta": desire_delta,
            "fear_delta": fear_delta,
            "disguise_delta": disguise_delta,
            "trigger": clean_text("；".join(trigger_notes))[:240],
            "action": clean_text(
                actor_action.get("resolved_intent")
                or actor_action.get("visible_behavior")
                or actor_action.get("action_intent", {}).get("description")
            )[:240],
        }
        return {
            "character_id": profile["character_id"],
            "dominant_drive": dominant_drive,
            "active_objective": active_objective,
            "active_fear": active_fear,
            "current_strategy": current_strategy,
            "desire_intensity": next_desire_intensity,
            "fear_intensity": next_fear_intensity,
            "attachment_focus": clean_text(
                current.get("attachment_focus")
                or (root_lookup.get("attachments") or [""])[0]
            ),
            "temptation_focus": clean_text(
                current.get("temptation_focus")
                or (root_lookup.get("temptations") or [""])[0]
            ),
            "disguise_pressure": self._intensity_shift(
                current.get("disguise_pressure"), disguise_delta
            ),
            "action_policy": deep_copy(action_policy),
            "confidence": quality.get("status")
                or root_lookup.get("coverage")
                or current.get("confidence", "unknown"),
            "last_trigger": history_entry["trigger"],
            "last_updated_by_event_id": "",
            "history": [
                *deep_copy(current.get("history", [])),
                history_entry,
            ][-12:],
        }

    def _dynamic_profile(self, character_id):
        if character_id in self.agent_by_character_id:
            return self._profile_with_current_capabilities(
                self.agent_by_character_id[character_id]
            )
        runtime_profile = self._runtime_npc_profiles().get(character_id)
        if runtime_profile:
            runtime_profile = self._runtime_npc_profile_with_autonomy(
                runtime_profile
            )
            return self._profile_with_admin_overrides(
                self._profile_with_current_capabilities(runtime_profile)
            )
        character = self.character_by_id[character_id]
        evidence = [
            {
                "source_chunk_id": item.get("source_chunk_id"),
                "source_text": clean_text(item.get("source_text")),
            }
            for item in character.get("evidence", [])
            if clean_text(item.get("source_text"))
        ][:8]
        profile = {
            "agent_id": "runtime_agent_" + character_id,
            "character_id": character_id,
            "canonical_name": character["canonical_name"],
            "profile_tier": "reference",
            "runtime_mode": "dynamic_reference_agent",
            "simulation_status": character.get("simulation_status", "minor"),
            "identity": {
                "canonical_name": character["canonical_name"],
                "aliases": character.get("aliases", []),
                "titles": character.get("titles", []),
                "forms": character.get("form_names", []),
                "temporary_identities": character.get(
                    "temporary_identities", []
                ),
                "canonical_identity_names": [
                    character["canonical_name"],
                    *character.get("aliases", []),
                ],
            },
            "identity_layers": deep_copy(character.get("identity_layers", {})),
            "core_motivation": deep_copy(character.get("core_motivation", {})),
            "state": {
                "background_summary": character.get("background_summary", ""),
                "background_evidence": character.get("background", [])[:8],
                "personality": character.get("personality", []),
                "goals": character.get("goals", []),
                "constraints": character.get("constraints", []),
                "speech_styles": character.get("speech_styles", []),
                "knowledge_scope": character.get("knowledge_scope", []),
            },
            "attributes": character.get("attributes", {}),
            "capabilities": {
                "abilities": character.get("abilities", []),
                "owned_items": character.get("owned_items", []),
                "used_items": character.get("used_items", []),
            },
            "relationships": [],
            "weak_relation_candidates": character.get("relationships", [])[:8],
            "metadata_relation_candidates": [],
            "world_context": {
                "knowledge_refs": [],
                "supported_retrieval_candidates": [],
            },
            "evidence_refs": evidence,
            "guardrails": {
                "evidence_only": True,
                "unsupported_fields_must_remain_unknown": True,
                "dynamic_reference_agent": True,
            },
        }
        return self._profile_with_admin_overrides(
            self._profile_with_current_capabilities(profile)
        )

    def agent_catalog(self):
        rows = []
        full_ids = set(self.agent_by_character_id)
        for character in self.character_db.get("characters", []):
            profile = self.agent_by_character_id.get(character["character_id"])
            tier = profile["profile_tier"] if profile else "reference"
            rows.append(
                {
                    "character_id": character["character_id"],
                    "canonical_name": character["canonical_name"],
                    "aliases": character.get("aliases", []),
                    "tier": tier,
                    "runtime_mode": (
                        profile["runtime_mode"]
                        if profile
                        else "dynamic_reference_agent"
                    ),
                    "notice": {
                        "full": "完整 Agent：证据较丰富，可持续独立模拟。",
                        "light": "轻量 Agent：仅在证据覆盖范围内行动，可按检索升级。",
                        "reference": "动态 Agent：信息较少，未知内容保持未知，进入场景后按需升级。",
                    }[tier],
                    "prebuilt": character["character_id"] in full_ids,
                    "simulation_status": character.get(
                        "simulation_status", "minor"
                    ),
                }
            )
        for character_id, profile in self._runtime_npc_profiles().items():
            rows.append(
                {
                    "character_id": character_id,
                    "canonical_name": profile.get("canonical_name", character_id),
                    "aliases": profile.get("identity", {}).get("aliases", []),
                    "tier": profile.get("profile_tier", "runtime"),
                    "runtime_mode": profile.get(
                        "runtime_mode",
                        "runtime_ambient_promoted_agent",
                    ),
                    "notice": "运行时 NPC：由持续互动的普通人物临时升级而来。",
                    "prebuilt": False,
                    "simulation_status": profile.get(
                        "simulation_status",
                        "local_runtime_npc",
                    ),
                }
            )
        return rows

    def _search_terms(self, text, profiles, scene=None):
        cleaned_query = (
            clean_text(text)
            + " "
            + clean_text((scene or {}).get("summary"))
            + " "
            + clean_text((scene or {}).get("scene_summary"))
        )
        terms = set(
            re.findall(
                "[\\w\u4e00-\u9fff]{2,}",
                cleaned_query,
            )
        )
        for sequence in re.findall("[\u4e00-\u9fff]{4,}", cleaned_query):
            for size in (2, 3, 4):
                for index in range(0, max(0, len(sequence) - size + 1)):
                    terms.add(sequence[index:index + size])
        if "爸爸" in cleaned_query:
            terms.add("父亲")
        if "爷爷" in cleaned_query:
            terms.add("老杰克")
        for profile in profiles:
            terms.add(profile["canonical_name"])
            terms.update(profile.get("identity", {}).get("aliases", []))
            for tag in profile.get("retrieval_tags", []):
                tag = clean_text(tag)
                if len(tag) >= 2 and tag in cleaned_query:
                    terms.add(tag)
        return {item.casefold() for item in terms if item}

    def _profile_source_snippets(self, profile):
        records = []

        def add_record(source, text, chunk_id="", weight=1, tags=None):
            text = clean_text(text)
            if not text:
                return
            records.append({
                "source": source,
                "source_chunk_id": str(chunk_id) if chunk_id not in (None, "") else "",
                "source_text": text,
                "tags": compact_list(tags or [], 20),
                "weight": weight,
                "character_id": profile.get("character_id"),
                "character_name": profile.get("canonical_name"),
            })

        base_tags = [
            profile.get("canonical_name", ""),
            *profile.get("identity", {}).get("aliases", [])[:8],
            *profile.get("identity", {}).get("titles", [])[:8],
        ]
        for item in profile.get("evidence_refs", []):
            add_record(
                "profile_evidence",
                item.get("source_text"),
                item.get("source_chunk_id"),
                weight=2,
                tags=base_tags,
            )
        for relation in profile.get("relationships", []):
            relation_tags = [
                relation.get("name", ""),
                relation.get("relation_type", ""),
                *base_tags,
            ]
            for item in relation.get("evidence", []):
                add_record(
                    "relationship_evidence",
                    item.get("source_text"),
                    item.get("source_chunk_id"),
                    weight=4,
                    tags=relation_tags,
                )
        for memory in profile.get("memories", []):
            for chunk_id in memory.get("source_chunk_ids", []) or [""]:
                add_record(
                    "event_ref",
                    memory.get("source_text") or memory.get("relation_summary"),
                    chunk_id,
                    weight=5,
                    tags=[
                        memory.get("relation_type", ""),
                        memory.get("source_name", ""),
                        memory.get("target_name", ""),
                        *base_tags,
                    ],
                )
        for item in profile.get("source_evidence_refs", []):
            for snippet in item.get("snippets", []):
                add_record(
                    "source_chunk_ref",
                    snippet,
                    item.get("source_chunk_id"),
                    weight=3,
                    tags=base_tags,
                )
        return records

    def _runtime_retrieval_packet(
        self,
        user_input,
        profiles,
        terms,
        limit=3,
        scene_override=None,
    ):
        candidates = []
        seen = set()

        def add_candidate(score, record):
            marker = (
                record.get("source"),
                record.get("source_chunk_id"),
                record.get("source_text"),
                record.get("character_id"),
                record.get("timeline_id"),
            )
            if marker in seen:
                return
            seen.add(marker)
            candidates.append((score, record))

        for profile in profiles:
            for record in self._profile_source_snippets(profile):
                haystack = " ".join([
                    record.get("source_text", ""),
                    record.get("character_name", ""),
                    *record.get("tags", []),
                ]).casefold()
                score = record.get("weight", 1)
                matched_terms = []
                for term in terms:
                    if term and term in haystack:
                        score += 3
                        matched_terms.append(term)
                source_text = record.get("source_text", "")
                if (
                    ("爸爸" in terms or "父亲" in terms)
                    and ("父亲" in source_text or "爸爸" in source_text)
                ):
                    score += 12
                    matched_terms.append("亲属询问")
                if "锻造" in terms and "锻造" in source_text:
                    score += 8
                    matched_terms.append("锻造")
                if not matched_terms and record.get("source") not in {
                    "relationship_evidence", "event_ref",
                }:
                    continue
                record = deep_copy(record)
                record["matched_terms"] = compact_list(matched_terms, 12)
                add_candidate(score, record)
        scene = scene_override or self.store.runtime.get("active_scene") or {}
        cursor = int(self.store.runtime.get("timeline_cursor", 0) or 0)
        timeline = self._timeline_nodes()
        for index, beat in enumerate(timeline):
            if not beat.get("system_generated"):
                continue
            distance = abs(index - cursor)
            if distance > 4:
                continue
            haystack = " ".join(
                [
                    beat.get("event", ""),
                    beat.get("default_outcome", ""),
                    *beat.get("participant_names", []),
                    *[
                        item.get("name", "")
                        for item in beat.get("artifact_refs", [])
                    ],
                    *[
                        item.get("name", "")
                        for item in beat.get("ability_refs", [])
                    ],
                ]
            ).casefold()
            matched_terms = [term for term in terms if term and term in haystack]
            participant_overlap = set(beat.get("participants", [])) & set(
                scene.get("participant_ids", [])
            )
            if not matched_terms and not participant_overlap:
                continue
            source_text = beat.get("default_outcome", "")
            if not source_text:
                continue
            score = 6 + max(0, 5 - distance) + len(matched_terms) * 2
            record = {
                "source": "canonical_scene_beat",
                "timeline_id": beat.get("timeline_id"),
                "source_chunk_id": str(
                    (beat.get("source_chunk_ids") or [""])[0]
                ),
                "source_text": source_text,
                "tags": compact_list(
                    [
                        beat.get("event", ""),
                        *beat.get("participant_names", []),
                        "scene_beat",
                    ],
                    20,
                ),
                "weight": score,
                "character_id": "",
                "character_name": "",
                "matched_terms": compact_list(matched_terms, 12),
                "visibility": "actor_visible_only_if_current_or_nearby",
            }
            add_candidate(score, record)
        candidates.sort(
            key=lambda item: (
                -item[0],
                int(item[1]["source_chunk_id"])
                if item[1].get("source_chunk_id", "").isdigit()
                else 10**9,
                item[1].get("source_text", ""),
            )
        )
        snippets = []
        per_character_count = defaultdict(int)
        used_source_texts = set()
        available_characters = {
            record.get("character_id")
            for _, record in candidates
            if record.get("character_id")
        }
        for score, record in candidates:
            source_marker = clean_text(record.get("source_text")).casefold()
            if source_marker in used_source_texts:
                continue
            character_id = record.get("character_id")
            if (
                len(available_characters) > 1
                and character_id
                and per_character_count[character_id] >= 2
            ):
                continue
            record["score"] = score
            snippets.append(record)
            used_source_texts.add(source_marker)
            if character_id:
                per_character_count[character_id] += 1
            if len(snippets) >= limit:
                break
        return {
            "enabled": True,
            "strategy": "hybrid_terms_graph_source_refs",
            "query": clean_text(user_input),
            "query_terms": sorted(terms)[:40],
            "source_snippets": snippets,
            "policy": (
                "Use retrieved snippets as source-grounded detail. If top "
                "snippets do not cover a claim, keep it uncertain instead of "
                "using outside story knowledge."
            ),
        }

    def _rag_query_plan(self, user_input, profiles, terms):
        scene = self.store.runtime.get("active_scene") or {}
        focus_id = scene.get("focus_character_id")
        planned = []
        for profile in profiles:
            character_id = profile["character_id"]
            if character_id == focus_id:
                access_tier = "active_focus"
            elif character_id in scene.get("participant_ids", []):
                access_tier = "active_nearby"
            else:
                access_tier = "cold_reference"
            needs = [
                "agent_profile_db",
                "runtime_agent_memory",
                "runtime_character_state",
                "runtime_relationship_db",
                "capability_and_item_db",
                "source_evidence_refs",
            ]
            if access_tier == "active_focus":
                needs.append("player_visible_scene_context")
            if access_tier == "active_nearby":
                needs.append("npc_perception_context")
            planned.append(
                {
                    "character_id": character_id,
                    "canonical_name": profile.get("canonical_name"),
                    "runtime_access_tier": access_tier,
                    "query_terms": sorted(terms)[:32],
                    "databases": needs,
                    "epistemic_policy": {
                        "may_see_future_canonical_anchors": False,
                        "may_see_other_private_memory": False,
                        "may_use_external_model_knowledge": False,
                        "may_use_gm_only_causal_notes": False,
                    },
                    "promotion_policy": {
                        "selected_by_user_becomes": "active_focus",
                        "nearby_or_repeated_companion_becomes": "active_nearby",
                        "cold_npc_gets_sidecar_when_entering_scene": True,
                    },
                }
            )
        return {
            "planner": "deterministic_step17_rag_query_planner",
            "input": clean_text(user_input),
            "global_query_terms": sorted(terms)[:48],
            "actor_plans": planned,
            "system_plans": {
                "gm_resolver": [
                    "actor_packets",
                    "runtime_state",
                    "current_and_nearby_canonical_anchors",
                    "branch_records",
                    "validator_results",
                ],
                "local_world_agent": [
                    "scene_state",
                    "runtime_resource_state",
                    "visible_actor_capabilities",
                    "local_environment",
                ],
                "global_world_agent": [
                    "committed_event",
                    "runtime_state",
                    "canonical_pressure",
                    "branch_records",
                ],
                "scene_renderer": [
                    "resolved_actions",
                    "visible_actor_packets",
                    "current_anchor_only",
                    "previous_visible_narrative",
                ],
            },
            "internal_agent_tools": [
                "character_root_lookup",
                "motivation_evidence_retriever",
                "graph_neighborhood_tool",
                "retrieval_quality_gate",
            ],
            "security_policy": {
                "actor_packets_are_epistemically_filtered": True,
                "future_anchors_are_system_only": True,
                "unretrieved_claims_must_remain_uncertain": True,
                "state_changes_require_validator_and_commit_event": True,
            },
        }

    def _visible_recent_events_for_actor(self, character_id):
        return [
            {
                "event_id": item["event_id"],
                "event_type": item.get("event_type"),
                "narration": item.get("narration", "")[:900],
                "revision_after": item.get("revision_after"),
            }
            for item in self.store.branch.get("events", [])[-8:]
            if character_id in item.get("visible_to", [])
            or character_id in item.get("participants", [])
            or character_id == item.get("player_id")
        ]

    def _known_concept_ids_for_actor(self, profile):
        character_id = profile["character_id"]
        known = {character_id}
        known.update(
            item.get("concept_id")
            for item in profile.get("world_context", {}).get(
                "knowledge_refs", []
            )
            if item.get("concept_id")
        )
        for key in ("abilities", "owned_items", "used_items"):
            known.update(
                item.get("entity_id")
                for item in profile.get("capabilities", {}).get(key, [])
                if item.get("entity_id")
            )
        known.update(
            item.get("entity_id")
            for item in profile.get("relationships", [])
            if item.get("entity_id")
        )
        known.update(
            self.store.runtime.get("knowledge_ledger", {}).get(
                character_id, []
            )
        )
        scene = self.store.runtime.get("active_scene") or {}
        known.update(scene.get("participant_ids", []))
        if scene.get("location_id"):
            known.add(scene["location_id"])
        return {item for item in known if item}

    def _knowledge_for_actor(self, profile, units):
        known = self._known_concept_ids_for_actor(profile)
        result = []
        supported = []
        for unit in units:
            entity_id = unit.get("entity_id")
            if entity_id not in known:
                continue
            if unit.get("model_status") == "trusted":
                result.append(deep_copy(unit))
            elif unit.get("model_status") == "supported":
                supported.append(deep_copy(unit))
        return result[:8], supported[:4]

    def _actor_story_spine(self, include_future=False):
        spine = self._story_spine_context()
        if include_future:
            return spine
        return {
            "timeline_cursor": spine.get("timeline_cursor"),
            "timeline_event_count": spine.get("timeline_event_count"),
            "current_anchor": spine.get("current_anchor", {}),
            "narrative_spine_state": spine.get("narrative_spine_state", {}),
            "control_contract": spine.get("control_contract", {}),
            "epistemic_note": (
                "Actor-facing packet excludes future canonical anchors. "
                "The current anchor is narrative pressure, not guaranteed knowledge."
            ),
        }

    def _actor_rag_packet(
        self,
        profile,
        user_input,
        terms,
        units,
        global_retrieval,
        access_tier,
        include_future=False,
    ):
        character_id = profile["character_id"]
        trusted, supported = self._knowledge_for_actor(profile, units)
        snippets = [
            item
            for item in global_retrieval.get("source_snippets", [])
            if item.get("character_id") in {character_id, None, ""}
        ][:5]
        relationships = profile.get("relationships", [])[:16]
        core_motivation = deep_copy(profile.get("core_motivation", {}))
        if core_motivation:
            core_motivation["current_trigger_analysis"] = (
                self._current_trigger_analysis(profile, core_motivation)
            )
        root_lookup = self._character_root_lookup(profile)
        graph_neighborhood = self._graph_neighborhood_tool(profile)
        motivation_evidence = self._motivation_evidence_retriever(
            profile,
            terms,
            global_retrieval,
            limit=10 if access_tier == "active_focus" else 7,
        )
        quality_gate = self._retrieval_quality_gate(
            root_lookup,
            motivation_evidence,
        )
        merged_snippets = []
        seen_snippets = set()
        for item in [
            *snippets,
            *motivation_evidence.get("evidence_snippets", []),
        ]:
            marker = (
                item.get("source"),
                item.get("source_chunk_id"),
                clean_text(item.get("source_text")),
            )
            if marker in seen_snippets:
                continue
            seen_snippets.add(marker)
            merged_snippets.append(item)
            if len(merged_snippets) >= 8:
                break
        packet = {
            "schema_version": STEP17_SCHEMA_VERSION,
            "layer": "Runtime Agent Knowledge DB",
            "character_id": character_id,
            "canonical_name": profile.get("canonical_name"),
            "runtime_access_tier": access_tier,
            "updated_revision": self.store.branch["head_revision"],
            "query": clean_text(user_input),
            "query_terms": sorted(terms)[:48],
            "identity": deep_copy(profile.get("identity", {})),
            "core_motivation": core_motivation,
            "current_runtime_state": deep_copy(
                self.store.runtime.get("character_runtime", {}).get(
                    character_id, {}
                )
            ),
            "motivation_runtime": deep_copy(
                self.store.runtime.get("motivation_runtime", {}).get(
                    character_id, {}
                )
            ),
            "capabilities": deep_copy(profile.get("capabilities", {})),
            "relationships": deep_copy(relationships),
            "trusted_knowledge": trusted,
            "supported_knowledge": supported,
            "source_snippets": deep_copy(merged_snippets),
            "internal_tools": {
                "character_root_lookup": root_lookup,
                "motivation_evidence_retriever": motivation_evidence,
                "graph_neighborhood_tool": graph_neighborhood,
                "retrieval_quality_gate": quality_gate,
            },
            "recent_visible_events": self._visible_recent_events_for_actor(
                character_id
            ),
            "recent_dialogue_turns": [
                item
                for item in self.store.runtime.get(
                    "recent_dialogue_turns", []
                )[-8:]
                if character_id in item.get("visible_to", [])
                or character_id == item.get("player_id")
            ],
            "memory": deep_copy(
                self.store.runtime.get("agent_memories", {}).get(
                    character_id, {}
                )
            ),
            "story_spine": self._actor_story_spine(include_future),
            "guardrails": {
                "may_see_future_canonical_anchors": bool(include_future),
                "may_see_other_private_memory": False,
                "external_story_knowledge_allowed": False,
                "unsupported_claims_must_remain_uncertain": True,
                "state_changes_require_commit_event": True,
            },
            "promotion_policy": {
                "sidecar_created_when_active": True,
                "can_be_promoted_if_user_selects_or_keeps_nearby": True,
                "promotion_does_not_grant_future_knowledge": True,
            },
        }
        return packet

    def _system_rag_packets(self, units, global_retrieval):
        spine = self._story_spine_context()
        return {
            "gm_resolver": {
                "story_spine": spine,
                "trusted_knowledge": [
                    item for item in units if item.get("model_status") == "trusted"
                ][: self.max_context_units],
                "supported_knowledge": [
                    item for item in units if item.get("model_status") == "supported"
                ][: self.max_context_units],
                "runtime_retrieval": global_retrieval,
                "authority": "causal_adjudication_not_character_knowledge",
            },
            "local_world_agent": {
                "scene": deep_copy(self.store.runtime.get("active_scene") or {}),
                "location_runtime": deep_copy(
                    self.store.runtime.get("location_runtime", {})
                ),
                "runtime_retrieval": global_retrieval,
                "authority": "local_environment_only",
            },
            "global_world_agent": {
                "story_spine": spine,
                "branch_records": deep_copy(
                    self.store.runtime.get("branch_records", [])[-12:]
                ),
                "authority": "offscreen_projection_after_trigger_only",
            },
            "scene_renderer": {
                "story_spine": self._actor_story_spine(include_future=False),
                "runtime_retrieval": global_retrieval,
                "authority": "render_visible_results_only",
            },
        }

    def _publish_agent_knowledge_dbs(self, agent_packets):
        current = deep_copy(
            self.store.runtime.get("runtime_agent_knowledge_dbs", {})
        )
        for character_id, packet in agent_packets.items():
            current[character_id] = packet
        self.store.runtime["runtime_agent_knowledge_dbs"] = current
        self.store._sync_sidecar_files()

    def _timeline_nodes(self):
        runtime_timeline = self.store.runtime.get("canonical_timeline")
        if runtime_timeline:
            return runtime_timeline
        return (
            self.world_db.get("canonical_timeline_db", {}).get(
                "timeline_nodes", []
            )
            or []
        )

    def _timeline_event_at(self, index):
        timeline = self._timeline_nodes()
        if not timeline:
            return {}
        index = max(0, min(int(index or 0), len(timeline) - 1))
        return deep_copy(timeline[index])

    def _story_spine_context(self, scene_override=None):
        timeline = self._timeline_nodes()
        cursor = int(self.store.runtime.get("timeline_cursor", 0) or 0)
        total = len(timeline)
        cursor = max(0, min(cursor, max(0, total - 1)))
        nearby = [
            {
                **deep_copy(item),
                "relative_position": index - cursor,
            }
            for index, item in enumerate(
                timeline[max(0, cursor - 2): min(total, cursor + 3)],
                start=max(0, cursor - 2),
            )
        ]
        scene = scene_override or self.store.runtime.get("active_scene") or {}
        return {
            "timeline_cursor": cursor,
            "timeline_event_count": total,
            "current_anchor": self._timeline_event_at(cursor),
            "nearby_anchors": nearby,
            "branch_records": deep_copy(
                self.store.runtime.get("branch_records", [])[-8:]
            ),
            "narrative_spine_state": deep_copy(
                self.store.runtime.get("narrative_spine", {})
            ),
            "control_contract": {
                "manual_actor_id": scene.get("focus_character_id"),
                "manual_actor_scope": (
                    "User input controls this character's attempted action "
                    "for the current turn."
                ),
                "auto_actor_ids": [
                    item
                    for item in scene.get("participant_ids", [])
                    if item != scene.get("focus_character_id")
                ],
                "auto_actor_scope": (
                    "Nearby NPC agents may observe, continue goals, react, "
                    "and propose actions within perception limits."
                ),
                "local_world_scope": (
                    "Local World Agent updates the current room or nearby "
                    "area, sensory state, positions, and local events."
                ),
                "gm_scope": (
                    "GM Resolver adjudicates success, consequences, causal "
                    "consistency, and canonical anchor status."
                ),
                "global_world_scope": (
                    "Global World Agent only runs for long time jumps, travel, "
                    "leaving the region, or high-impact consequences."
                ),
                "canonical_policy": (
                    "The original plot is pressure and expectation, not a "
                    "forced script. If player action resolves, alters, or "
                    "prevents the current anchor, advance the cursor."
                ),
            },
        }

    def build_context_packet(self, user_input, profiles, scene_override=None):
        scene = scene_override or self.store.runtime.get("active_scene") or {}
        terms = self._search_terms(user_input, profiles, scene)
        expanded_terms = set(terms)
        for profile in profiles:
            expanded_terms.update(self._motivation_terms(profile))
        terms = {
            item for item in expanded_terms
            if clean_text(item)
        }
        query_plan = self._rag_query_plan(user_input, profiles, terms)
        units = self._search_knowledge_units(
            terms,
            limit=self.max_context_units,
        )
        global_retrieval = self._runtime_retrieval_packet(
            user_input, profiles, terms, limit=8, scene_override=scene
        )
        access_by_character_id = {
            item["character_id"]: item.get("runtime_access_tier", "cold_reference")
            for item in query_plan.get("actor_plans", [])
        }
        focus_id = scene.get("focus_character_id")
        agent_packets = {
            profile["character_id"]: self._actor_rag_packet(
                profile,
                user_input,
                terms,
                units,
                global_retrieval,
                access_by_character_id.get(
                    profile["character_id"], "cold_reference"
                ),
                include_future=False,
            )
            for profile in profiles
        }
        self._publish_agent_knowledge_dbs(agent_packets)
        system_packets = self._system_rag_packets(units, global_retrieval)
        return {
            "scene": scene,
            "state_revision": self.store.branch["head_revision"],
            "query_plan": query_plan,
            "runtime_retrieval": global_retrieval,
            "rag_orchestration": {
                "enabled": True,
                "actor_packet_ids": list(agent_packets),
                "agent_packets": agent_packets,
                "system_packets": system_packets,
                "focus_character_id": focus_id,
                "policy": {
                    "all_agent_reasoning_uses_query_planner": True,
                    "actor_packets_exclude_future_anchors": True,
                    "system_packets_may_use_future_pressure_for_adjudication": True,
                    "cold_npcs_receive_sidecar_when_active": True,
                },
            },
            "story_spine": self._story_spine_context(scene_override=scene),
            "trusted_knowledge": [
                item for item in units if item.get("model_status") == "trusted"
            ],
            "supported_knowledge": [
                item for item in units if item.get("model_status") == "supported"
            ],
            "recent_events": [
                {
                    "event_id": item["event_id"],
                    "event_type": item["event_type"],
                    "narration": item.get("narration", ""),
                    "revision_after": item["revision_after"],
                }
                for item in self.store.branch["events"][-8:]
            ],
            "recent_dialogue_turns": deep_copy(
                self.store.runtime.get("recent_dialogue_turns", [])[-8:]
            ),
        }

    def _agent_prompt(self, profile, user_input, context):
        memory = self.store.runtime.get("agent_memories", {}).get(
            profile["character_id"], {}
        )
        agent_context = deep_copy(
            context.get("rag_orchestration", {})
            .get("agent_packets", {})
            .get(profile["character_id"], context)
        )
        agent_context["recent_dialogue_turns"] = [
            item
            for item in context.get("recent_dialogue_turns", [])
            if profile["character_id"] in item.get("visible_to", [])
            or profile["character_id"] == item.get("player_id")
        ]
        system = """
你是小说模拟中的一个独立角色 Agent。你只能根据人物卡、当前场景、
该角色可见的记忆和本轮检索包行动，不得用模型对原著的外部常识补全。
必须同时输出台词与行动意图。角色可以尝试行动，但不能自行宣布高影响
行动成功；结果由 World Validator 和 GM 决定。未知信息必须保持未知。
只输出 JSON。
""".strip()
        payload = {
            "character": {
                "character_id": profile["character_id"],
                "canonical_name": profile["canonical_name"],
                "profile_tier": profile["profile_tier"],
                "identity": profile.get("identity", {}),
                "identity_layers": profile.get("identity_layers", {}),
                "core_motivation": profile.get("core_motivation", {}),
                "state": profile.get("state", {}),
                "capabilities": profile.get("capabilities", {}),
                "relationships": profile.get("relationships", [])[:12],
                "retrieval_tags": profile.get("retrieval_tags", [])[:32],
                "source_chunk_refs": profile.get("source_chunk_refs", [])[:24],
                "event_refs": profile.get("event_refs", [])[:12],
                "needs_runtime_retrieval": profile.get(
                    "needs_runtime_retrieval", False
                ),
                "guardrails": profile.get("guardrails", {}),
            },
            "runtime_memory": memory,
            "context": agent_context,
            "user_input": user_input,
        }
        user = f"""
根据输入生成本角色这一轮的反应：
{json.dumps(payload, ensure_ascii=False)}

输出格式：
{{
  "dialogue": "角色说出的话；可以为空字符串",
  "action_intent": {{
    "action_type": "通用动作类型",
    "description": "角色想做什么，不声明未经裁定的结果",
    "impact_level": "dialogue|minor_action|state_change|high_impact",
    "target_concept_ids": [],
    "ability_concept_id": "",
    "artifact_concept_id": "",
    "candidate_rule_ids": [],
    "proposed_state_changes": [
      {{
        "subject_id": "concept id",
        "field": "status|location_id|holder_id|condition|availability|relationship|knowledge|presence|state.xxx|custom.xxx",
        "before": "unknown or expected value",
        "after": "new value"
      }}
    ]
  }},
  "concept_refs": [
    {{
      "surface": "原文名称",
      "intent": "character|location|artifact|ability|goal|organization|event|rule",
      "concept_id": "resolved concept id"
    }}
  ],
  "claims": [
    {{
      "subject_concept_id": "concept id",
      "predicate": "state.field or relation",
      "object_or_value": "value",
      "knowledge_source": "self_background|current_scene|told_by_character|rag|memory|unknown",
      "source_event_id": "",
      "temporal_scope": "past|current|future|unknown"
    }}
  ],
  "private_reasoning_summary": "不含隐藏思维，只写角色动机的一句摘要"
}}
""".strip()
        return system, user

    def _call_json(self, system, user, max_tokens=2200):
        try:
            raw = self.call_llm(
                system,
                user,
                temperature=0.2,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
        except TypeError:
            raw = self.call_llm(
                system,
                user,
                temperature=0.2,
                max_tokens=max_tokens,
            )
        return extract_json_object(raw)

    def _normalize_proposal(self, profile, payload):
        action = payload.get("action_intent")
        if not isinstance(action, dict):
            action = {}
        impact = clean_text(action.get("impact_level")).lower()
        if impact not in NON_STATEFUL_IMPACT_LEVELS | STATEFUL_IMPACT_LEVELS:
            impact = "dialogue"
        return {
            "agent_id": profile["agent_id"],
            "character_id": profile["character_id"],
            "canonical_name": profile["canonical_name"],
            "dialogue": clean_text(payload.get("dialogue")),
            "action_intent": {
                "action_type": clean_text(action.get("action_type")) or "wait",
                "description": clean_text(action.get("description")),
                "impact_level": impact,
                "target_concept_ids": [
                    clean_text(item)
                    for item in action.get("target_concept_ids", [])
                    if clean_text(item)
                ],
                "ability_concept_id": clean_text(
                    action.get("ability_concept_id")
                ),
                "artifact_concept_id": clean_text(
                    action.get("artifact_concept_id")
                ),
                "candidate_rule_ids": [
                    clean_text(item)
                    for item in action.get("candidate_rule_ids", [])
                    if clean_text(item)
                ],
                "proposed_state_changes": [
                    item
                    for item in action.get("proposed_state_changes", [])
                    if isinstance(item, dict)
                ],
            },
            "concept_refs": [
                item
                for item in payload.get("concept_refs", [])
                if isinstance(item, dict)
            ],
            "claims": [
                item
                for item in payload.get("claims", [])
                if isinstance(item, dict)
            ],
            "private_reasoning_summary": clean_text(
                payload.get("private_reasoning_summary")
            ),
        }

    def _gm_adjudicate(self, user_input, proposals, validations, context):
        system = """
你是小说模拟的 GM/局部推演 Agent。你不能替角色重写人格，也不能泄露
Validator 内部错误。根据角色意图、已通过或待裁定的检查、当前状态和证据，
决定动作结果。高影响动作没有规则时只能给出尝试、部分结果、失败或需要
后续事件的结果，不能无依据地让世界巨变。只输出 JSON。
""".strip()
        public_validations = [
            {
                "character_id": proposal["character_id"],
                "status": validation["status"],
                "check_outcomes": [
                    {
                        "category": item["category"],
                        "outcome": item["outcome"],
                    }
                    for item in validation["checks"]
                ],
            }
            for proposal, validation in zip(proposals, validations)
        ]
        user = f"""
用户输入：{user_input}
角色意图：{json.dumps(proposals, ensure_ascii=False)}
验证摘要：{json.dumps(public_validations, ensure_ascii=False)}
场景上下文：{json.dumps(context, ensure_ascii=False)}

返回：
{{
  "narration": "面向用户的剧情叙述，不提 Validator 或 JSON",
  "dialogue": [
    {{"speaker_id": "character id", "speaker_name": "name", "text": "台词"}}
  ],
  "resolved_actions": [
    {{
      "actor_id": "character id",
      "description": "实际发生的动作",
      "outcome": "success|partial|failed|deferred",
      "state_changes": []
    }}
  ],
  "event_type": "scene_interaction",
  "impact_level": "dialogue|minor_action|state_change|high_impact",
  "elapsed_minutes": "本轮对话和动作实际消耗的整数分钟",
  "duration_reason": "耗时依据",
  "visible_to": ["character ids"],
  "world_projection_needed": false
}}
""".strip()
        return self._call_json(system, user, max_tokens=2600)

    def _world_project(self, event, context):
        world_context = (
            context.get("rag_orchestration", {})
            .get("system_packets", {})
            .get("global_world_agent", context)
        )
        system = """
你是世界推演 Agent，负责当前场景之外的连锁反应。只依据已裁定事件、
只读世界事实和当前 simulation state 推演。不要替当前场景角色说台词。
没有足够证据时不产生变化。只输出 JSON。
""".strip()
        user = f"""
已裁定事件：{json.dumps(event, ensure_ascii=False)}
相关世界上下文：{json.dumps(world_context, ensure_ascii=False)}
返回：
{{
  "narration_append": "",
  "state_changes": [],
  "affected_concept_ids": [],
  "summary": "",
  "additional_elapsed_minutes": "场外连锁反应额外消耗的整数分钟，否则为0"
}}
""".strip()
        return self._call_json(system, user, max_tokens=1400)

    def _event_from_adjudication(
        self,
        user_input,
        proposals,
        adjudication,
        validations,
    ):
        state_changes = []
        for item in adjudication.get("resolved_actions", []):
            if item.get("outcome") in {"success", "partial"}:
                state_changes.extend(
                    change
                    for change in item.get("state_changes", [])
                    if isinstance(change, dict)
                )
        scene = self.store.runtime.get("active_scene") or {}
        event_payload = {
            "event_type": clean_text(adjudication.get("event_type"))
            or "scene_interaction",
            "turn": scene.get("turn", 0) + 1,
            "user_input": clean_text(user_input),
            "proposals": proposals,
        }
        elapsed_minutes = bounded_int(
            adjudication.get("elapsed_minutes"),
            default=1,
            minimum=1,
        )
        return {
            "event_id": "event_" + uuid.uuid4().hex[:16],
            "idempotency_key": stable_hash(event_payload),
            "event_type": event_payload["event_type"],
            "impact_level": clean_text(adjudication.get("impact_level"))
            or "minor_action",
            "status": "completed",
            "participants": scene.get("participant_ids", []),
            "visible_to": adjudication.get(
                "visible_to", scene.get("participant_ids", [])
            ),
            "narration": clean_text(adjudication.get("narration")),
            "dialogue": [
                item
                for item in adjudication.get("dialogue", [])
                if isinstance(item, dict)
            ],
            "action_intents": [
                {
                    "actor_id": item["character_id"],
                    **item["action_intent"],
                }
                for item in proposals
            ],
            "resolved_actions": adjudication.get("resolved_actions", []),
            "state_changes": state_changes,
            "elapsed_minutes": elapsed_minutes,
            "duration_reason": clean_text(
                adjudication.get("duration_reason")
            ),
            "clock_transition": self.store.clock_after_minutes(
                elapsed_minutes
            ),
            "validator_records": [
                {
                    "validation_id": item["validation_id"],
                    "status": item["status"],
                    "correction_action": item["correction_action"],
                }
                for item in validations
            ],
            "evidence_refs": compact_list(
                [
                    evidence
                    for validation in validations
                    for check in validation["checks"]
                    for evidence in check.get("evidence_refs", [])
                ],
                24,
            ),
            "created_at": utc_now(),
        }

    def _event_validation(self, event, validations):
        if any(item["status"] == "blocked" for item in validations):
            candidate_changes = []
        else:
            candidate_changes = event.get("state_changes", [])
        allowed_changes = []
        checks = []
        for change in candidate_changes:
            subject_id = clean_text(change.get("subject_id"))
            field = clean_text(change.get("field"))
            subject_exists = bool(
                subject_id in self.store.runtime.get("entity_states", {})
                or subject_id in self.store.runtime.get("artifact_states", {})
                or subject_id in self.store.runtime.get("resource_states", {})
                or subject_id in self.validator.concept_candidates
            )
            if not subject_exists or not field:
                checks.append(
                    {
                        "category": "gm_event_commit",
                        "outcome": "blocked",
                        "internal_reason": (
                            "GM state change has an unknown subject or empty field."
                        ),
                        "evidence_refs": [],
                    }
                )
                continue
            allowed_changes.append(change)
            checks.append(
                {
                    "category": "gm_event_commit",
                    "outcome": "allowed",
                    "internal_reason": (
                        "GM result references a known concept and a supported "
                        "state field; commit preconditions are checked atomically."
                    ),
                    "evidence_refs": event.get("evidence_refs", []),
                }
            )
        event["state_changes"] = allowed_changes
        if not checks:
            checks.append(
                {
                    "category": "gm_event_commit",
                    "outcome": "allowed",
                    "internal_reason": "Event has no mutable world effect.",
                    "evidence_refs": event.get("evidence_refs", []),
                }
            )
        discarded_effect_count = sum(
            item["outcome"] == "blocked" for item in checks
        )
        return {
            "validation_id": "validation_" + uuid.uuid4().hex[:16],
            "status": "allowed",
            "commit_allowed": True,
            "checks": checks,
            "correction_action": (
                "commit_event"
                if not discarded_effect_count
                else "discard_invalid_effect_and_commit_safe_event"
            ),
            "user_visible_reason": "",
        }

    def _summarize_memories(self, profiles, force=False):
        if (
            not force
            and self.store.branch["head_revision"]
            % self.memory_summary_interval
        ):
            return
        for profile in profiles:
            character_id = profile["character_id"]
            memory = self.store.runtime.get("agent_memories", {}).get(
                character_id, {}
            )
            event_ids = set(memory.get("recent_event_ids", []))
            events = [
                {
                    "event_id": item["event_id"],
                    "narration": item.get("narration", ""),
                    "dialogue": [
                        line
                        for line in item.get("dialogue", [])
                        if line.get("speaker_id") == character_id
                        or character_id in item.get("visible_to", [])
                    ],
                }
                for item in self.store.branch["events"]
                if item["event_id"] in event_ids
            ][-8:]
            if not events:
                continue
            fragments = []
            previous = clean_text(memory.get("summary", ""))
            if previous:
                fragments.append(previous[-500:])
            for event in events[-4:]:
                narration = clean_text(event.get("narration", ""))
                dialogue = "；".join(
                    clean_text(line.get("text"))
                    for line in event.get("dialogue", [])
                    if clean_text(line.get("text"))
                )
                row = "；".join(
                    item for item in [narration[:220], dialogue[:160]]
                    if item
                )
                if row:
                    fragments.append(row)
            summary = clean_text(" / ".join(fragments))[-900:]
            self.store.update_memory_summary(character_id, summary)

    def _nearby_state_description(self):
        scene = self.store.runtime.get("active_scene") or {}
        location_id = scene.get("location_id")
        location = self.location_by_id.get(location_id, {})
        participant_rows = []
        for character_id in scene.get("participant_ids", []):
            character = self.character_by_id.get(character_id, {})
            state = self.store.runtime.get("character_runtime", {}).get(
                character_id, {}
            )
            participant_rows.append(
                {
                    "character_id": character_id,
                    "name": character.get("canonical_name", character_id),
                    "activity": clean_text(state.get("current_activity")),
                    "posture": clean_text(state.get("posture")),
                    "mood": clean_text(state.get("mood")),
                    "availability": clean_text(state.get("availability")),
                }
            )
        location_state = self.store.runtime.get(
            "location_runtime", {}
        ).get(location_id, {})
        return {
            "location_id": location_id,
            "location_name": location.get("name")
            or location.get("canonical_name")
            or clean_text(scene.get("scene_summary"))
            or "当前位置",
            "scene_summary": clean_text(scene.get("scene_summary")),
            "characters": participant_rows,
            "sensory_environment": {
                key: value
                for key, value in location_state.items()
                if key
                in {
                    "weather",
                    "light",
                    "sound",
                    "smell",
                    "temperature",
                    "visibility",
                }
                and value
            },
            "active_events": deep_copy(
                self.store.runtime.get("active_events", [])[-5:]
            ),
            "clock": deep_copy(
                self.store.runtime.get("simulation_clock", {})
            ),
        }

    def world_admin_snapshot(self, character_limit=40):
        scene = self.store.runtime.get("active_scene") or {}
        focus_id = scene.get("focus_character_id")
        characters = []
        for character in self.character_db.get("characters", [])[:character_limit]:
            character_id = character["character_id"]
            runtime_state = self.store.runtime.get("character_runtime", {}).get(
                character_id,
                {},
            )
            motivation = self.store.runtime.get("motivation_runtime", {}).get(
                character_id,
                {},
            )
            overrides = self.store.runtime.get("admin_profile_overrides", {}).get(
                character_id,
                {},
            )
            if (
                character_id == focus_id
                or character_id in scene.get("participant_ids", [])
                or runtime_state.get("current_activity")
                or motivation.get("dominant_drive")
                or overrides
            ):
                characters.append(
                    {
                        "character_id": character_id,
                        "canonical_name": character.get("canonical_name"),
                        "runtime": runtime_state,
                        "motivation_runtime": motivation,
                        "admin_profile_overrides": overrides,
                    }
                )
        if not characters:
            for character in self.character_db.get("characters", [])[:12]:
                characters.append(
                    {
                        "character_id": character["character_id"],
                        "canonical_name": character.get("canonical_name"),
                        "runtime": self.store.runtime.get(
                            "character_runtime",
                            {},
                        ).get(character["character_id"], {}),
                        "motivation_runtime": self.store.runtime.get(
                            "motivation_runtime",
                            {},
                        ).get(character["character_id"], {}),
                        "admin_profile_overrides": self.store.runtime.get(
                            "admin_profile_overrides",
                            {},
                        ).get(character["character_id"], {}),
                    }
                )
        recent_events = [
            {
                "event_id": event.get("event_id"),
                "event_type": event.get("event_type"),
                "player_input": event.get("player_input", ""),
                "narration": clean_text(event.get("narration", ""))[:700],
                "elapsed_minutes": event.get("elapsed_minutes", 0),
                "backend_stage": event.get("backend_stage", ""),
            }
            for event in self.store.branch.get("events", [])[-8:]
        ]
        return {
            "active_scene": scene,
            "focus_character_id": focus_id,
            "nearby_state": self._nearby_state_description(),
            "story_spine": self._story_spine_context(),
            "current_canonical_event": self.current_canonical_event(),
            "recent_events": recent_events,
            "characters": characters,
            "admin_profile_overrides": deep_copy(
                self.store.runtime.get("admin_profile_overrides", {})
            ),
            "world_admin_log": deep_copy(
                self.store.runtime.get("world_admin_log", [])[-20:]
            ),
        }

    def world_admin_chat(self, message, apply_changes=True):
        message = clean_text(message)
        if not message:
            raise ValueError("World admin message is empty.")
        snapshot = self.world_admin_snapshot()
        system = """
你是 World Admin Console，不是任何角色。你拥有上帝视角，可以帮助用户查询
当前剧情、解释 runtime 状态、定位角色、查看动机，也可以按用户要求修改
运行时角色特征。不要假装成玩家控制角色，不要用第一人称角色视角回答。
修改必须显式写入 JSON 字段；没有明确要求修改时只回答，不要更改世界。

可修改字段：
1. admin_profile_overrides: character_id -> {personality/goals/constraints/state/core_motivation/admin_note}
2. character_runtime_updates: character_id -> 当前活动、姿态、心情、短期目标等运行时状态
3. motivation_runtime_updates: character_id -> dominant_drive、active_objective、active_fear、current_strategy 等动态动机

只输出 JSON。
""".strip()
        user = json.dumps(
            {
                "admin_message": message,
                "snapshot": snapshot,
                "output_schema": {
                    "reply": "给用户看的管理员回复",
                    "plot_summary": "当前剧情摘要",
                    "admin_profile_overrides": {},
                    "character_runtime_updates": {},
                    "motivation_runtime_updates": {},
                    "notes": [],
                },
            },
            ensure_ascii=False,
        )
        payload = self._call_json(system, user, max_tokens=2200)
        if not isinstance(payload, dict):
            payload = {"reply": str(payload)}
        runtime_updates = {}
        for source_key, runtime_key in (
            ("admin_profile_overrides", "admin_profile_overrides"),
            ("character_runtime_updates", "character_runtime"),
            ("motivation_runtime_updates", "motivation_runtime"),
        ):
            value = payload.get(source_key, {})
            if isinstance(value, dict) and value:
                runtime_updates[runtime_key] = deep_copy(value)
        has_admin_changes = bool(runtime_updates)
        if has_admin_changes:
            log_entry = {
                "created_at": utc_now(),
                "message": message,
                "reply": clean_text(payload.get("reply")),
                "changed": True,
            }
            runtime_updates["world_admin_log"] = [
                *deep_copy(self.store.runtime.get("world_admin_log", [])),
                log_entry,
            ][-100:]
        commit = None
        if apply_changes and has_admin_changes:
            event_id = "event_" + uuid.uuid4().hex[:16]
            event = {
                "event_id": event_id,
                "idempotency_key": stable_hash(
                    {
                        "event_id": event_id,
                        "branch": self.store.branch["branch_id"],
                        "revision": self.store.branch["head_revision"],
                        "admin_message": message,
                    }
                ),
                "event_type": "world_admin_intervention",
                "impact_level": (
                    "state_change"
                    if any(
                        key in runtime_updates
                        for key in (
                            "admin_profile_overrides",
                            "character_runtime",
                            "motivation_runtime",
                        )
                    )
                    else "dialogue"
                ),
                "status": "completed",
                "participants": [],
                "visible_to": [],
                "narration": clean_text(payload.get("plot_summary")),
                "dialogue": [],
                "action_intents": [],
                "resolved_actions": [],
                "state_changes": [],
                "runtime_updates": runtime_updates,
                "elapsed_minutes": 0,
                "duration_reason": "world admin console",
                "clock_transition": self.store.clock_after_minutes(0),
                "backend_stage": "world_admin_console",
                "created_at": utc_now(),
            }
            validation = {
                "validation_id": "validation_" + uuid.uuid4().hex[:16],
                "status": "allowed",
                "commit_allowed": True,
                "checks": [
                    {
                        "category": "world_admin",
                        "outcome": "allowed",
                        "internal_reason": "Admin console changes are explicit user-authorized runtime overrides.",
                        "evidence_refs": [],
                    }
                ],
                "correction_action": "commit_event",
                "user_visible_reason": "",
            }
            commit = self.store.commit_event(event, validation)
        return {
            "reply": clean_text(payload.get("reply")),
            "plot_summary": clean_text(payload.get("plot_summary")),
            "notes": payload.get("notes", []),
            "applied": bool(commit),
            "commit": commit,
            "snapshot_after": self.world_admin_snapshot(),
        }

    def create_manual_save(self, progress_callback=None):
        scene = self.store.runtime.get("active_scene") or {}
        participant_ids = scene.get("participant_ids", [])
        profiles = [
            self._dynamic_profile(character_id)
            for character_id in participant_ids
            if self._is_known_character_id(character_id)
        ]
        self._progress(
            progress_callback, 15, "正在整理最近几轮对话"
        )
        self._summarize_memories(profiles, force=True)
        self._progress(
            progress_callback, 55, "正在生成下次进入时的记忆摘要"
        )
        recent_turns = deep_copy(
            self.store.runtime.get("recent_dialogue_turns", [])[-8:]
        )
        nearby_state = self._nearby_state_description()
        summary = ""
        if recent_turns:
            try:
                payload = self._call_json(
                    (
                        "你负责生成小说模拟的玩家恢复摘要。根据最近对话和"
                        "当前场景，用第二人称写一段简短、明确的中文摘要。"
                        "说明玩家刚做了什么、重要回应、未解决事项。"
                        "不要逐轮复述，不要补充未知信息。只输出 JSON："
                        '{"summary":"..."}。'
                    ),
                    json.dumps(
                        {
                            "recent_turns": recent_turns,
                            "nearby_state": nearby_state,
                        },
                        ensure_ascii=False,
                    ),
                    max_tokens=700,
                )
                summary = clean_text(payload.get("summary"))
            except Exception:
                summary = ""
        if not summary:
            latest = recent_turns[-1] if recent_turns else {}
            summary = clean_text(latest.get("narration"))[:900]
        if not summary:
            summary = clean_text(
                nearby_state.get("scene_summary")
            ) or "你回到了上次保存的场景。"
        snapshot = {
            "saved_at": utc_now(),
            "revision": self.store.branch["head_revision"],
            "focus_character_id": scene.get("focus_character_id"),
            "summary": summary,
            "nearby_state": nearby_state,
            "recent_turn_count": len(recent_turns),
        }
        self.store.runtime["recovery_snapshot"] = snapshot
        self.store.branch["checkpoints"].append(
            {
                "revision": self.store.branch["head_revision"],
                "label": "manual_save",
                "created_at": snapshot["saved_at"],
            }
        )
        self._progress(
            progress_callback, 85, "正在同步角色与世界运行数据库"
        )
        self.store.save()
        self._progress(progress_callback, 100, "存档完成")
        return deep_copy(snapshot)

    def run_world_tick(self, reason="scheduled_world_tick"):
        scene = self.store.runtime.get("active_scene") or {}
        profiles = [
            self._dynamic_profile(character_id)
            for character_id in scene.get("participant_ids", [])
        ]
        context = self.build_context_packet(reason, profiles)
        seed_event = {
            "event_id": "event_" + uuid.uuid4().hex[:16],
            "event_type": "world_tick",
            "impact_level": "state_change",
            "participants": scene.get("participant_ids", []),
            "narration": clean_text(reason),
            "trigger_reason": clean_text(reason),
            "state_changes": [],
            "created_at": utc_now(),
        }
        projection = self._world_project(seed_event, context)
        elapsed_minutes = bounded_int(
            projection.get("additional_elapsed_minutes"),
            default=0,
        )
        event = {
            **seed_event,
            "idempotency_key": stable_hash(
                {
                    "reason": reason,
                    "revision": self.store.branch["head_revision"],
                    "clock": self.store.runtime.get("simulation_clock", {}),
                }
            ),
            "status": "completed",
            "visible_to": scene.get("participant_ids", []),
            "narration": clean_text(
                projection.get("narration_append")
                or projection.get("summary")
                or "世界在当前场景之外继续演化。"
            ),
            "dialogue": [],
            "action_intents": [],
            "resolved_actions": [],
            "state_changes": [
                item
                for item in projection.get("state_changes", [])
                if isinstance(item, dict)
            ],
            "world_projection": projection,
            "backend_stage": "world_agent_projection",
            "evidence_refs": [],
            "elapsed_minutes": elapsed_minutes,
            "duration_reason": "世界 Agent 场外推演",
            "clock_transition": self.store.clock_after_minutes(
                elapsed_minutes
            ),
        }
        validation = self._event_validation(event, [])
        commit = self.store.commit_event(event, validation)
        return {
            "event": event,
            "commit": commit,
            "internal_validation": validation,
        }

    def run_turn(self, user_input):
        scene = self.store.runtime.get("active_scene")
        if not scene:
            raise RuntimeError("Start a scene before running a turn.")
        focus_character_id = clean_text(scene.get("focus_character_id"))
        participant_ids = [
            character_id
            for character_id in scene.get("participant_ids", [])
            if character_id != focus_character_id
        ][: self.max_nearby_agents]
        control_modes = self.store.runtime.get("agent_control", {})
        profiles = [
            self._dynamic_profile(item)
            for item in participant_ids
            if control_modes.get(item, "AUTO") != "MANUAL"
        ]
        context = self.build_context_packet(user_input, profiles)
        rag_ids = [
            item["entity_id"]
            for item in [
                *context["trusted_knowledge"],
                *context["supported_knowledge"],
            ]
        ]
        proposals = []
        validations = []
        for profile in profiles:
            system, prompt = self._agent_prompt(profile, user_input, context)
            try:
                payload = self._call_json(system, prompt)
            except Exception as error:
                payload = {
                    "dialogue": "",
                    "action_intent": {
                        "action_type": "wait",
                        "description": "保持观察",
                        "impact_level": "minor_action",
                        "target_concept_ids": [],
                        "proposed_state_changes": [],
                    },
                    "concept_refs": [],
                    "claims": [],
                    "private_reasoning_summary": clean_text(error),
                }
            proposal = self._normalize_proposal(profile, payload)
            validation = self.validator.validate(
                proposal,
                profile["character_id"],
                self.store,
                rag_ids,
            )
            proposals.append(proposal)
            validations.append(validation)

        assisted_pairs = [
            (proposal, validation)
            for proposal, validation in zip(proposals, validations)
            if control_modes.get(proposal["character_id"], "AUTO")
            == "ASSISTED"
        ]
        auto_pairs = [
            (proposal, validation)
            for proposal, validation in zip(proposals, validations)
            if control_modes.get(proposal["character_id"], "AUTO") == "AUTO"
        ]
        adjudication = self._gm_adjudicate(
            user_input,
            [item[0] for item in auto_pairs],
            [item[1] for item in auto_pairs],
            context,
        )
        event = self._event_from_adjudication(
            user_input,
            [item[0] for item in auto_pairs],
            adjudication,
            [item[1] for item in auto_pairs],
        )
        pending_actions = deep_copy(
            self.store.runtime.get("pending_actions", [])
        )
        for proposal, validation in assisted_pairs:
            pending_actions.append(
                {
                    "pending_id": "pending_" + uuid.uuid4().hex[:12],
                    "character_id": proposal["character_id"],
                    "canonical_name": proposal["canonical_name"],
                    "dialogue": proposal.get("dialogue", ""),
                    "action_intent": proposal["action_intent"],
                    "validation_status": validation["status"],
                    "created_at": utc_now(),
                }
            )
        event["pending_actions_after"] = pending_actions[-50:]
        event["backend_stage"] = "local_gm_adjudication"
        if adjudication.get("world_projection_needed") or event[
            "impact_level"
        ] == "high_impact":
            projection = self._world_project(event, context)
            event["narration"] = clean_text(
                " ".join(
                    [
                        event.get("narration", ""),
                        projection.get("narration_append", ""),
                    ]
                )
            )
            event["state_changes"].extend(
                item
                for item in projection.get("state_changes", [])
                if isinstance(item, dict)
            )
            event["world_projection"] = projection
            event["backend_stage"] = "world_agent_projection"
            additional_minutes = bounded_int(
                projection.get("additional_elapsed_minutes"),
                default=0,
            )
            if additional_minutes:
                event["elapsed_minutes"] += additional_minutes
                event["clock_transition"] = self.store.clock_after_minutes(
                    event["elapsed_minutes"]
                )

        final_validation = self._event_validation(
            event,
            [item[1] for item in auto_pairs],
        )
        commit_result = self.store.commit_event(event, final_validation)
        self._summarize_memories(profiles)
        return {
            "event": event,
            "commit": commit_result,
            "state_revision": self.store.branch["head_revision"],
            "branch_id": self.store.branch["branch_id"],
            "internal_validation": {
                "proposal_validations": validations,
                "event_validation": final_validation,
            },
            "assisted_suggestions": [
                item[0] for item in assisted_pairs
            ],
        }


class ImmersiveSimulationOrchestrator(SimulationOrchestrator):
    """Local-first novel runtime with separate simulation and prose stages."""

    def __init__(self, *args, min_narrative_chars=1500, **kwargs):
        super().__init__(*args, **kwargs)
        self.min_narrative_chars = max(0, int(min_narrative_chars))
        self.location_by_id = {
            item["entity_id"]: item
            for item in self.world_db.get("world_sections", {}).get(
                "locations", []
            )
        }
        self.canonical_timeline = self._build_canonical_timeline()
        self.world_concept_by_id = {}
        for section in ("abilities", "artifacts"):
            for item in self.world_db.get("world_sections", {}).get(
                section, []
            ):
                self.world_concept_by_id[item["entity_id"]] = item

    @staticmethod
    def _progress(callback, value, label):
        if callback:
            callback(int(value), clean_text(label))

    @staticmethod
    def _response_language(text):
        text = str(text or "")
        han = sum("\u4e00" <= char <= "\u9fff" for char in text)
        latin = sum(char.isascii() and char.isalpha() for char in text)
        if han >= max(2, latin):
            return "简体中文"
        return "与玩家输入相同的语言"

    @staticmethod
    def _is_passive_continue_input(text):
        normalized = clean_text(text).casefold()
        passive_inputs = {
            "",
            "继续",
            "继续剧情",
            "继续故事",
            "继续推进",
            "下一步",
            "然后呢",
            "看看周围",
            "观察",
            "观察并让局势自然推进",
            "随便",
            "无",
            "没事",
            "continue",
            "continue story",
            "next",
            "go on",
            "wait",
            "look around",
            "i wait",
            "i pause",
        }
        if normalized in passive_inputs:
            return True
        return normalized in {
            "i pause and pay attention to what is happening around me.",
            "i pause and pay attention to what is happening around me",
        }

    def _last_visible_narrative(self):
        for event in reversed(self.store.branch["events"]):
            if (
                event.get("narration")
                and event.get("event_type")
                in {"scene_opening_rendered", "immersive_scene_turn"}
            ):
                return event["narration"]
        return ""

    @staticmethod
    def _impact_rank(value):
        return {
            "dialogue": 0,
            "minor_action": 1,
            "state_change": 2,
            "high_impact": 3,
        }.get(clean_text(value).lower(), 1)

    def _mentions_profile(self, text, profile):
        haystack = clean_text(text)
        if not haystack:
            return False
        names = {
            profile.get("canonical_name", ""),
            *profile.get("aliases", []),
            *profile.get("forms", []),
            *profile.get("identity", {}).get("aliases", []),
            *profile.get("identity", {}).get("forms", []),
        }
        return any(name and name in haystack for name in names)

    def _runtime_character_state(self, character_id):
        state = deep_copy(RUNTIME_CHARACTER_DEFAULTS)
        state.update(
            deep_copy(
                self.store.runtime.get("character_runtime", {}).get(
                    character_id, {}
                )
            )
        )
        state["character_id"] = character_id
        return state

    def _character_current_location(self, character_id):
        runtime_state = self._runtime_character_state(character_id)
        location_id = clean_text(
            runtime_state.get("current_location")
            or runtime_state.get("location_id")
        )
        if location_id:
            return location_id
        entity_state = (
            self.store.runtime.get("entity_states", {}).get(character_id, {})
        )
        mutable = entity_state.get("mutable_fields", {})
        return clean_text(
            mutable.get("current_location") or mutable.get("location_id")
        )

    def _location_name(self, location_id):
        location_id = clean_text(location_id)
        if not location_id:
            return ""
        runtime_location = self.store.runtime.get("location_runtime", {}).get(
            location_id, {}
        )
        for key in ("location_name", "name", "summary"):
            value = clean_text(runtime_location.get(key))
            if value:
                return value
        location = self.location_by_id.get(location_id, {})
        return clean_text(
            location.get("canonical_name")
            or location.get("name")
            or location.get("display_name")
            or location_id
        )

    @staticmethod
    def _text_mentions_village(text):
        text = clean_text(text)
        return any(
            term in text
            for term in (
                "村里",
                "村子",
                "村庄",
                "村口",
                "村民",
                "路人",
                "行人",
                "农人",
                "乡民",
                "庄户",
                "附近村",
                "村落",
            )
        )

    @staticmethod
    def _turn_text(*parts):
        return clean_text(
            "；".join(clean_text(part) for part in parts if clean_text(part))
        )

    def _runtime_location_record(self, location_id, name, origin_id="", reason=""):
        return {
            "location_id": clean_text(location_id),
            "location_name": clean_text(name) or clean_text(location_id),
            "name": clean_text(name) or clean_text(location_id),
            "origin_location_id": clean_text(origin_id),
            "summary": clean_text(name) or clean_text(location_id),
            "source": "runtime_scene_transition",
            "reason": clean_text(reason),
            **deep_copy(RUNTIME_LOCATION_DEFAULTS),
        }

    def _nearby_village_location(self, origin_location_id):
        origin_location_id = clean_text(origin_location_id)
        origin_name = self._location_name(origin_location_id)
        label = (
            f"{origin_name}附近村落"
            if origin_name and "村" not in origin_name
            else "附近村落"
        )
        location_id = (
            "runtime_location_"
            + stable_hash(
                {
                    "origin": origin_location_id or "unknown_origin",
                    "kind": "nearby_village",
                    "label": label,
                }
            )[:12]
        )
        return self._runtime_location_record(
            location_id,
            label,
            origin_id=origin_location_id,
            reason="玩家行动进入或已经处于附近村落，但原著数据库没有独立地点实体。",
        )

    def _normalize_location_for_turn(
        self,
        candidate_id,
        text,
        origin_id,
        candidate_name="",
    ):
        candidate_id = clean_text(candidate_id)
        origin_id = clean_text(origin_id)
        candidate_name = clean_text(candidate_name)
        reference_id = candidate_id or origin_id
        reference_name = candidate_name or self._location_name(reference_id)
        if self._text_mentions_village(text) and "村" not in reference_name:
            return self._nearby_village_location(origin_id or candidate_id)
        if candidate_id:
            return self._runtime_location_record(
                candidate_id,
                candidate_name or self._location_name(candidate_id),
                origin_id=origin_id,
                reason="LLM 或裁定层提交了角色位置变化。",
            )
        return self._runtime_location_record(
            origin_id,
            self._location_name(origin_id),
            origin_id=origin_id,
            reason="沿用当前场景地点。",
        )

    @staticmethod
    def _state_change_subject(change):
        return clean_text(
            change.get("subject_id")
            or change.get("target_id")
            or change.get("target")
            or change.get("entity_id")
            or change.get("character_id")
        )

    @staticmethod
    def _state_change_field(change):
        return clean_text(
            change.get("field")
            or change.get("property")
            or change.get("change_type")
        )

    @staticmethod
    def _state_change_after(change):
        if "after" in change:
            return change.get("after")
        if "new_value" in change:
            return change.get("new_value")
        return change.get("value")

    def _location_record_from_turn_outputs(
        self,
        player_id,
        player_intent=None,
        local_world=None,
        resolution=None,
        raw_user_input="",
        scene_location_id="",
    ):
        current_id = (
            self._character_current_location(player_id)
            or clean_text(scene_location_id)
        )
        candidate_name = ""
        runtime_state = self._runtime_character_state(player_id)
        text = self._turn_text(
            raw_user_input,
            (player_intent or {}).get("injected_thought", ""),
            (player_intent or {}).get("resolved_intent", ""),
            (player_intent or {}).get("thought_assimilation", ""),
            runtime_state.get("current_activity", ""),
            runtime_state.get("short_term_goal", ""),
            runtime_state.get("attention_target", ""),
        )
        candidate_id = ""
        scene_transition = (local_world or {}).get("scene_transition")
        if isinstance(scene_transition, dict):
            candidate_id = clean_text(
                scene_transition.get("location_id")
                or scene_transition.get("new_location_id")
            )
            candidate_name = clean_text(
                scene_transition.get("location_name")
                or scene_transition.get("name")
            )
        for change in (resolution or {}).get("state_changes", []) or []:
            if not isinstance(change, dict):
                continue
            if self._state_change_subject(change) != player_id:
                continue
            field = self._state_change_field(change)
            if "location" in field:
                candidate_id = clean_text(self._state_change_after(change))
        for update in (local_world or {}).get("npc_position_updates", []) or []:
            if not isinstance(update, dict):
                continue
            character_id = clean_text(
                update.get("character_id")
                or update.get("entity_id")
                or update.get("actor_id")
            )
            if character_id != player_id:
                continue
            candidate_id = clean_text(
                update.get("new_location_id")
                or update.get("location_id")
                or update.get("destination_location_id")
            )
        return self._normalize_location_for_turn(
            candidate_id,
            text,
            current_id,
            candidate_name=candidate_name,
        )

    def _scene_with_effective_location(self, scene, location_record):
        result = deep_copy(scene or {})
        location_id = clean_text(location_record.get("location_id"))
        if location_id:
            result["location_id"] = location_id
        if location_record.get("location_name"):
            result["location_name"] = location_record.get("location_name")
        return result

    @staticmethod
    def _availability_is_active(value):
        value = clean_text(value).lower()
        if not value:
            return True
        dormant_terms = (
            "dormant",
            "background",
            "sleeping",
            "offscreen",
            "left_scene",
            "away",
            "unavailable",
            "dead",
            "inactive",
        )
        return not any(term in value for term in dormant_terms)

    def _same_location(self, left, right):
        left = clean_text(left)
        right = clean_text(right)
        return bool(left and right and left == right)

    def _active_nearby_character_ids(
        self,
        user_input,
        player_id,
        scene,
        effective_location_id,
    ):
        effective_location_id = clean_text(effective_location_id)
        scene = scene or {}
        scene_ids = [
            item
            for item in scene.get("participant_ids", [])
            if item != player_id
        ]
        runtime_ids = [
            character_id
            for character_id, state in self.store.runtime.get(
                "character_runtime", {}
            ).items()
            if character_id != player_id
            and self._is_known_character_id(character_id)
            and self._same_location(
                clean_text(state.get("current_location")),
                effective_location_id,
            )
        ]
        candidate_ids = compact_list([*scene_ids, *runtime_ids], 64)
        selected = []
        for character_id in candidate_ids:
            if not self._is_known_character_id(character_id):
                continue
            state = self._runtime_character_state(character_id)
            if not self._availability_is_active(state.get("availability")):
                continue
            location_id = self._character_current_location(character_id)
            if effective_location_id and location_id:
                if location_id != effective_location_id:
                    continue
            elif effective_location_id and not location_id:
                continue
            profile = self._dynamic_profile(character_id)
            mentioned = self._mentions_profile(user_input, profile)
            in_scene = character_id in scene_ids
            if not mentioned and not in_scene and len(selected) >= self.max_nearby_agents:
                continue
            selected.append(character_id)
        return compact_list(selected, self.max_nearby_agents)

    @staticmethod
    def _action_text_for_follow_check(npc_action):
        intent = npc_action.get("action_intent", {}) if isinstance(npc_action, dict) else {}
        return clean_text(
            "；".join(
                [
                    npc_action.get("visible_behavior", ""),
                    npc_action.get("goal", ""),
                    npc_action.get("dialogue", ""),
                    intent.get("action_type", ""),
                    intent.get("description", ""),
                ]
            )
        )

    def _npc_explicitly_stays_with_player(self, npc_action, user_input):
        text = self._action_text_for_follow_check(npc_action)
        if self._mentions_profile(user_input, {"canonical_name": npc_action.get("canonical_name", "")}):
            return True
        return any(
            term in text
            for term in (
                "跟随",
                "随行",
                "同行",
                "陪同",
                "追上",
                "追赶",
                "拦住",
                "拉住",
                "同行移动",
                "护送",
            )
        )

    @staticmethod
    def _ambient_reaction_text(reaction):
        if not isinstance(reaction, dict):
            return ""
        return clean_text(
            "；".join(
                clean_text(reaction.get(key))
                for key in ("speaker_label", "visible_behavior", "dialogue")
                if clean_text(reaction.get(key))
            )
        )

    @staticmethod
    def _input_refers_to_recent_ambient(text, label):
        text = clean_text(text)
        label = clean_text(label)
        if label and label in text:
            return True
        pronouns = {"他", "她", "那人", "那个人", "老汉", "此人", "对方"}
        interaction_terms = {
            "问",
            "说",
            "聊",
            "威胁",
            "逼问",
            "质问",
            "审问",
            "吓",
            "恐吓",
            "咨询",
            "追问",
            "拦",
            "放过",
        }
        return any(term in text for term in pronouns) and any(
            term in text for term in interaction_terms
        )

    def _recent_ambient_candidates(self, limit=4):
        candidates = []
        current_location = clean_text(
            (self.store.runtime.get("active_scene") or {}).get("location_id")
        )
        for event in reversed(self.store.branch.get("events", [])):
            local_world = event.get("local_world", {})
            if not isinstance(local_world, dict):
                continue
            runtime_scene = (
                event.get("runtime_updates", {}).get("active_scene", {})
                if isinstance(event.get("runtime_updates"), dict)
                else {}
            )
            event_location = clean_text(
                runtime_scene.get("location_id")
                or (
                    local_world.get("scene_transition", {})
                    if isinstance(local_world.get("scene_transition"), dict)
                    else {}
                ).get("location_id")
                or (self.store.runtime.get("active_scene") or {}).get("location_id")
            )
            for reaction in reversed(local_world.get("ambient_npc_reactions", [])):
                if not isinstance(reaction, dict):
                    continue
                label = clean_text(reaction.get("speaker_label"))
                if not label:
                    continue
                text = self._ambient_reaction_text(reaction)
                candidates.append(
                    {
                        "label": label,
                        "text": text,
                        "event_id": event.get("event_id", ""),
                        "location_id": event_location or current_location,
                    }
                )
                if len(candidates) >= limit:
                    return candidates
        return candidates

    def _ensure_runtime_npc(
        self,
        label,
        location_id,
        memory_text="",
        seed_event_id="",
        availability="active_nearby_npc",
    ):
        location_id = clean_text(location_id) or clean_text(
            (self.store.runtime.get("active_scene") or {}).get("location_id")
        )
        character_id = self._existing_runtime_npc_id(label, location_id)
        if not character_id:
            character_id = self._runtime_npc_id(label, location_id)
        profiles = self._runtime_npc_profiles()
        if character_id not in profiles:
            profiles[character_id] = self._runtime_npc_profile(
                character_id,
                label,
                location_id,
                seed_event_id=seed_event_id,
                memory_text=memory_text,
            )
        else:
            profile = profiles[character_id]
            attributes = profile.setdefault("attributes", {})
            attributes.setdefault("location_id", location_id)
            attributes["last_location_id"] = location_id
            if seed_event_id:
                attributes["last_source_event_id"] = clean_text(seed_event_id)
            if memory_text:
                memories = profile.setdefault("memories", [])
                if not any(
                    clean_text(item.get("source_text")) == clean_text(memory_text)
                    for item in memories
                    if isinstance(item, dict)
                ):
                    memories.append(
                        {
                            "source_text": clean_text(memory_text),
                            "relation_summary": clean_text(memory_text),
                            "source_event_id": clean_text(seed_event_id),
                        }
                    )
                profile["memories"] = memories[-12:]
        runtime = self.store.runtime.setdefault("character_runtime", {})
        existing = runtime.get(character_id, {})
        runtime[character_id] = {
            **deep_copy(RUNTIME_CHARACTER_DEFAULTS),
            **deep_copy(existing),
            "character_id": character_id,
            "current_location": location_id,
            "current_activity": clean_text(memory_text) or existing.get(
                "current_activity",
                "正在与玩家角色互动",
            ),
            "availability": availability,
            "short_term_goal": existing.get(
                "short_term_goal",
                "在当前威胁下保全自己，必要时顺从、拖延、误导或寻找逃离机会",
            ),
            "known_information": compact_list(
                [
                    *existing.get("known_information", []),
                    clean_text(memory_text),
                ],
                16,
            ),
        }
        memory = self.store.runtime.setdefault("agent_memories", {}).setdefault(
            character_id,
            {
                "recent_event_ids": [],
                "summary": "",
                "last_revision": self.store.branch.get("head_revision", 0),
            },
        )
        if seed_event_id:
            memory["recent_event_ids"] = compact_list(
                [*memory.get("recent_event_ids", []), seed_event_id],
                24,
            )
        if memory_text and memory_text not in clean_text(memory.get("summary")):
            memory["summary"] = clean_text(
                "；".join(
                    item
                    for item in [memory.get("summary", ""), memory_text]
                    if clean_text(item)
                )
            )[-1200:]
        return character_id

    def _promote_referenced_ambient_npcs(self, user_input, player_id, scene):
        scene = scene or {}
        candidates = self._recent_ambient_candidates()
        explicit = [
            candidate
            for candidate in candidates
            if clean_text(candidate.get("label"))
            and clean_text(candidate.get("label")) in clean_text(user_input)
        ]
        if explicit:
            selected_candidates = explicit[:1]
        elif candidates and self._input_refers_to_recent_ambient(
            user_input,
            candidates[0].get("label"),
        ):
            selected_candidates = candidates[:1]
        else:
            selected_candidates = []
        promoted_ids = []
        for candidate in selected_candidates:
            character_id = self._ensure_runtime_npc(
                candidate.get("label"),
                candidate.get("location_id") or scene.get("location_id"),
                memory_text=candidate.get("text"),
                seed_event_id=candidate.get("event_id"),
            )
            promoted_ids.append(character_id)
        promoted_ids = compact_list(promoted_ids, self.max_nearby_agents)
        if promoted_ids:
            participants = compact_list(
                [
                    player_id,
                    *scene.get("participant_ids", []),
                    *promoted_ids,
                ],
                self.max_nearby_agents + 1,
            )
            scene["participant_ids"] = participants
            active_scene = self.store.runtime.get("active_scene")
            if isinstance(active_scene, dict):
                active_scene["participant_ids"] = participants
        return promoted_ids

    def _group_context_packet(self, user_input, player_intent, scene):
        scene = scene or {}
        location_id = clean_text(scene.get("location_id"))
        location_name = clean_text(scene.get("location_name"))
        runtime_location = self.store.runtime.get("location_runtime", {}).get(
            location_id,
            {},
        )
        text = self._turn_text(
            user_input,
            player_intent.get("resolved_intent", ""),
            player_intent.get("thought_assimilation", ""),
            scene.get("summary", ""),
            location_name,
            location_id,
            runtime_location.get("ambient_sound", ""),
            runtime_location.get("ongoing_events", []),
        )
        groups = [
            (
                "village_people",
                "附近村民",
                ("村", "村民", "村落", "茅屋", "庄户", "乡民", "犬吠"),
            ),
            (
                "market_crowd",
                "市集人群",
                ("市集", "市场", "商贩", "摊", "街市", "买卖", "行人"),
            ),
            (
                "soldiers",
                "附近士兵",
                ("士兵", "官兵", "军队", "巡逻", "守卫", "卫兵", "兵卒"),
            ),
            (
                "guards_servants",
                "侍从与护卫",
                ("侍从", "仆从", "护卫", "宫人", "衙役", "随从"),
            ),
            (
                "demons",
                "附近小妖",
                ("小妖", "妖兵", "妖怪", "洞府", "巡山", "喽啰"),
            ),
            (
                "pilgrims_or_monks",
                "附近僧众香客",
                ("僧众", "香客", "寺", "庙", "行僧", "和尚"),
            ),
            (
                "general_crowd",
                "周围人群",
                ("人群", "群众", "百姓", "路人", "围观", "众人"),
            ),
        ]
        matched = []
        for group_type, label, terms in groups:
            if (
                group_type == "village_people"
                and "离开村" in text
                and "村" not in location_name
                and not any(term in text for term in ("村民", "茅屋", "庄户", "乡民", "犬吠"))
            ):
                continue
            if any(term in text for term in terms):
                matched.append(
                    {
                        "group_type": group_type,
                        "label": label,
                        "terms": [term for term in terms if term in text][:6],
                    }
                )
        if not matched:
            return {"should_run": False, "groups": [], "text": text}
        trigger_terms = (
            "威胁",
            "恐吓",
            "打",
            "杀",
            "火",
            "妖",
            "追",
            "喊",
            "哭",
            "逃",
            "闯",
            "搜",
            "找",
            "问",
            "赶路",
            "进入",
            "离开",
            "等待",
            "继续",
        )
        visible_pressure = any(term in text for term in trigger_terms)
        should_run = bool(
            visible_pressure
            or self._is_passive_continue_input(user_input)
            or self._text_mentions_village(text)
        )
        return {
            "should_run": should_run,
            "groups": matched[:2],
            "text": text,
            "location_id": location_id,
            "location_name": location_name or self._location_name(location_id),
        }

    def _group_controller_agent(
        self,
        user_input,
        player_intent,
        npc_actions,
        elapsed_minutes,
        context,
    ):
        scene = (context or {}).get("scene") or self.store.runtime.get("active_scene") or {}
        packet = self._group_context_packet(user_input, player_intent, scene)
        if not packet.get("should_run"):
            return {
                "ran": False,
                "policy": "lazy_group_controller",
                "resource_mode": "deterministic_no_llm",
                "groups": [],
                "reason": "当前场景未检测到需要统一模拟的群众/村民/士兵等群体。",
            }
        text = packet.get("text", "")
        action_text = self._turn_text(
            text,
            *[
                self._action_text_for_follow_check(item)
                for item in npc_actions
                if isinstance(item, dict)
            ],
        )
        threat = any(
            term in action_text
            for term in (
                "威胁",
                "恐吓",
                "杀",
                "妖",
                "火",
                "惨",
                "抓",
                "拽",
                "哭",
                "惊",
                "逃",
            )
        )
        inquiry = any(term in action_text for term in ("问", "打听", "询问", "搜寻", "找"))
        movement = any(
            term in action_text
            for term in ("进入", "离开", "经过", "赶路", "继续", "跟着", "带路")
        )
        groups = []
        runtime_groups = self.store.runtime.setdefault("group_runtime", {})
        for item in packet.get("groups", []):
            label = item.get("label") or "周围人群"
            group_id = "runtime_group_" + stable_hash(
                {
                    "label": label,
                    "location_id": packet.get("location_id"),
                    "group_type": item.get("group_type"),
                }
            )[:16]
            previous = runtime_groups.get(group_id, {})
            if threat:
                mood = "恐慌警觉"
                goal = "避开危险、保护同伴，并把异常消息传给附近的人。"
                visible = (
                    f"{label}不再只是背景声；靠近动静的人压低声音互相提醒，"
                    "门窗逐渐合上，胆小者往阴影和屋舍后退。"
                )
                pressure = "群体恐慌会让消息扩散，也可能引来更多旁观或守卫。"
            elif inquiry:
                mood = "戒备观望"
                goal = "判断来者意图，交换传闻，同时避免被卷入冲突。"
                visible = (
                    f"{label}远远观察着问话与搜寻，低声交换传闻，"
                    "没有人愿意第一个走近。"
                )
                pressure = "群体传闻会影响后续 NPC 的警惕与可获得线索。"
            elif movement:
                mood = "避让警觉"
                goal = "给强势角色让路，并把异常路线记在心里。"
                visible = (
                    f"{label}察觉队伍移动后本能地让开道路，"
                    "视线追着他们消失的方向。"
                )
                pressure = "群体会把移动方向转化成局部消息。"
            else:
                mood = previous.get("mood", "低声观望")
                goal = "维持日常秩序，同时留意异常。"
                visible = f"{label}维持着低声的日常活动，但注意力已经被现场变化牵动。"
                pressure = "群体注意力会让场景不再完全静止。"
            groups.append(
                {
                    "group_id": group_id,
                    "label": label,
                    "group_type": item.get("group_type", "general_crowd"),
                    "location_id": packet.get("location_id"),
                    "mood": mood,
                    "goal": goal,
                    "visible_behavior": visible,
                    "dialogue": "",
                    "pressure": pressure,
                    "source_terms": item.get("terms", []),
                    "state_update": {
                        "mood": mood,
                        "current_activity": visible,
                        "last_pressure": pressure,
                        "last_seen_player_intent": clean_text(
                            player_intent.get("resolved_intent")
                        )[:240],
                    },
                }
            )
        return {
            "ran": True,
            "policy": "common_sense_group_controller",
            "resource_mode": "deterministic_no_llm",
            "trigger_reason": "检测到当前地点存在群体语境，统一模拟其常识性反应。",
            "elapsed_minutes": elapsed_minutes,
            "groups": groups,
        }

    @staticmethod
    def _group_controller_ran(group_controller):
        return bool(
            isinstance(group_controller, dict)
            and group_controller.get("ran")
            and group_controller.get("groups")
        )

    def _merge_group_controller_into_local_world(self, local_world, group_controller):
        if not self._group_controller_ran(group_controller):
            return local_world
        local_world = local_world if isinstance(local_world, dict) else {}
        local_world["group_controller"] = deep_copy(group_controller)
        reactions = local_world.setdefault("ambient_npc_reactions", [])
        new_events = local_world.setdefault("new_events", [])
        world_changes = local_world.setdefault("world_changes", [])
        existing_reactions = {
            (
                clean_text(item.get("speaker_label")),
                clean_text(item.get("visible_behavior")),
            )
            for item in reactions
            if isinstance(item, dict)
        }
        existing_events = {
            clean_text(item if isinstance(item, str) else item.get("description", ""))
            for item in new_events
        }
        for group in group_controller.get("groups", []):
            label = clean_text(group.get("label")) or "周围人群"
            visible = clean_text(group.get("visible_behavior"))
            if visible and (label, visible) not in existing_reactions:
                reactions.append(
                    {
                        "speaker_label": label,
                        "visible_behavior": visible,
                        "dialogue": clean_text(group.get("dialogue")),
                        "source": "group_controller",
                        "is_group": True,
                        "group_id": group.get("group_id"),
                    }
                )
            event_text = clean_text(group.get("pressure"))
            if event_text and event_text not in existing_events:
                new_events.append(event_text)
                world_changes.append(event_text)
                existing_events.add(event_text)
        return local_world

    def _agent_wake_plan(self, user_input, player_intent, profiles, elapsed_minutes, scene=None):
        passive = self._is_passive_continue_input(user_input)
        impact = self._impact_rank(
            player_intent.get("impact_level") or player_intent.get("action_type")
        )
        action_type = clean_text(player_intent.get("action_type")).lower()
        stateful = impact >= 2 or action_type in {
            "state_change",
            "high_impact",
            "travel",
            "sleep",
            "leave_region",
            "fast_forward",
            "internal_doubt",
            "refusal",
            "resistance",
            "avoidance",
            "change_plan",
            "reject_order",
        }
        wake_all_npcs = bool(
            passive
            or elapsed_minutes >= 5
            or impact >= 2
            or self._intent_implies_canon_divergence(player_intent)
        )
        max_auto_npcs = self.max_nearby_agents if wake_all_npcs else min(2, self.max_nearby_agents)
        selected = []
        for profile in profiles:
            if self._mentions_profile(user_input, profile):
                selected.append(profile)
                continue
            if wake_all_npcs:
                selected.append(profile)
                continue
            if len(selected) < max_auto_npcs:
                selected.append(profile)
        selected = selected[:max_auto_npcs]
        should_run_local_world = bool(
            passive
            or elapsed_minutes > 0
            or stateful
            or self._intent_implies_canon_divergence(player_intent)
        )
        group_context = self._group_context_packet(user_input, player_intent, scene or {})
        should_run_group_controller = bool(group_context.get("should_run"))
        should_run_gm = bool(
            stateful
            or should_run_local_world
            or should_run_group_controller
            or self._intent_implies_canon_divergence(player_intent)
        )
        return {
            "policy": "lazy_multi_agent_scheduler",
            "passive_continue": passive,
            "impact_rank": impact,
            "stateful_or_divergent": stateful,
            "wake_all_npcs": wake_all_npcs,
            "selected_npc_ids": [item["character_id"] for item in selected],
            "skipped_npc_ids": [
                item["character_id"]
                for item in profiles
                if item["character_id"] not in {p["character_id"] for p in selected}
            ],
            "should_run_local_world": should_run_local_world,
            "should_run_group_controller": should_run_group_controller,
            "group_controller_context": {
                key: value
                for key, value in group_context.items()
                if key in {"groups", "location_id", "location_name"}
            },
            "should_run_gm": should_run_gm,
            "reason": (
                "高影响/时间推进/继续/偏离原著会唤醒更多 Agent；普通小动作只唤醒相关或少量附近角色；检测到人群/村民/士兵等群体语境时唤醒 Group Controller。"
            ),
        }

    def _baseline_concept_card(self, concept_id, fallback=None):
        concept = self.world_concept_by_id.get(concept_id, {})
        fallback = fallback or {}
        descriptions = [
            clean_text(item)
            for item in concept.get("descriptions", [])
            if clean_text(item)
        ]
        evidence = fallback.get("evidence", [])
        evidence_summary = next(
            (
                clean_text(item.get("relation_summary"))
                for item in evidence
                if clean_text(item.get("relation_summary"))
            ),
            "",
        )
        return {
            "concept_id": concept_id,
            "name": clean_text(
                concept.get("canonical_name")
                or fallback.get("name")
                or concept_id
            ),
            "concept_type": clean_text(
                concept.get("entity_type")
                or fallback.get("entity_type")
            ),
            "summary": descriptions[0] if descriptions else evidence_summary,
            "details": compact_list(descriptions[1:4], 3),
            "attributes": deep_copy(concept.get("attributes", {})),
            "source": "world_db" if concept else "character_evidence",
        }

    def _baseline_physiology(self, character_id):
        character = self.character_by_id.get(character_id, {})
        texts = [
            clean_text(character.get("background_summary")),
            *[
                clean_text(item.get("source_text"))
                for item in character.get("evidence", [])
                if clean_text(item.get("source_text"))
            ],
        ]
        joined = " ".join(texts)
        result = deep_copy(RUNTIME_CHARACTER_DEFAULTS["physiology"])
        if any(marker in joined for marker in ("女孩", "女学生", "少女")):
            result["sex"] = "女"
        elif any(marker in joined for marker in ("男孩", "男学生", "少年")):
            result["sex"] = "男"
        if "女孩" in joined or "男孩" in joined or "孩子" in joined:
            result["apparent_age"] = "儿童或少年阶段"
        species_patterns = re.findall(
            r"(?:种族|本体|真实身份)[是为：:\s]*([\u4e00-\u9fff]{1,12})",
            joined,
        )
        if species_patterns:
            result["species"] = species_patterns[0]
        return result

    def _baseline_profile_status(self, character_id):
        profile = self._dynamic_profile(character_id)
        state = profile.get("state", {})
        capabilities = profile.get("capabilities", {})
        runtime = deep_copy(RUNTIME_CHARACTER_DEFAULTS)
        runtime.update(
            deep_copy(
                self.store.runtime.get("character_runtime", {}).get(
                    character_id, {}
                )
            )
        )
        baseline_physiology = self._baseline_physiology(character_id)
        physiology = deep_copy(baseline_physiology)
        physiology.update(
            {
                key: value
                for key, value in runtime.get("physiology", {}).items()
                if value not in (None, "", [], {})
            }
        )
        runtime["physiology"] = physiology
        ability_cards = [
            self._baseline_concept_card(
                item.get("entity_id"), item
            )
            for item in capabilities.get("abilities", [])
            if item.get("entity_id")
        ]
        item_rows = [
            *capabilities.get("owned_items", []),
            *capabilities.get("used_items", []),
        ]
        item_cards = [
            self._baseline_concept_card(
                item.get("entity_id"), item
            )
            for item in item_rows
            if item.get("entity_id")
        ]
        return {
            "character_id": character_id,
            "canonical_name": profile["canonical_name"],
            "profile_tier": profile["profile_tier"],
            "runtime_mode": profile["runtime_mode"],
            "identity": deep_copy(profile.get("identity", {})),
            "background_summary": clean_text(
                state.get("background_summary")
            ),
            "personality": deep_copy(state.get("personality", [])),
            "goals": deep_copy(state.get("goals", [])),
            "constraints": deep_copy(state.get("constraints", [])),
            "relationships": deep_copy(
                profile.get("relationships", [])[:12]
            ),
            "knowledge_scope": deep_copy(
                state.get("knowledge_scope", [])
            ),
            "runtime": runtime,
            "abilities": ability_cards,
            "items": item_cards,
        }

    def character_status_snapshot(self, character_id):
        snapshot = self._baseline_profile_status(character_id)
        cache = self.store.runtime.get("world_knowledge_cache", {})
        for group in ("abilities", "items"):
            snapshot[group] = [
                {
                    **item,
                    **deep_copy(cache.get(item["concept_id"], {})),
                }
                for item in snapshot[group]
            ]
        return snapshot

    def active_status_snapshots(self):
        scene = self.store.runtime.get("active_scene") or {}
        focus_id = scene.get("focus_character_id")
        rows = []
        for character_id in scene.get("participant_ids", []):
            profile = self._dynamic_profile(character_id)
            if (
                character_id == focus_id
                or profile.get("profile_tier") == "full"
            ):
                rows.append(self.character_status_snapshot(character_id))
        return rows

    def _world_cache_updates(self, character_ids, generated_updates=None):
        current = self.store.runtime.get("world_knowledge_cache", {})
        updates = {}
        for character_id in character_ids:
            profile = self._dynamic_profile(character_id)
            capabilities = profile.get("capabilities", {})
            for item in [
                *capabilities.get("abilities", []),
                *capabilities.get("owned_items", []),
                *capabilities.get("used_items", []),
            ]:
                concept_id = item.get("entity_id")
                if not concept_id or concept_id in current:
                    continue
                card = self._baseline_concept_card(concept_id, item)
                if card.get("summary"):
                    updates[concept_id] = card
        for item in generated_updates or []:
            if not isinstance(item, dict):
                continue
            concept_id = clean_text(item.get("concept_id"))
            if concept_id and concept_id not in current:
                updates[concept_id] = {
                    "concept_id": concept_id,
                    "name": clean_text(item.get("name")),
                    "concept_type": clean_text(
                        item.get("concept_type")
                    ),
                    "summary": clean_text(item.get("summary")),
                    "details": compact_list(
                        item.get("details", []), 4
                    ),
                    "source": "local_world_agent",
                }
        return updates

    @staticmethod
    def _source_orders(record):
        values = []
        try:
            values.append(int(record.get("first_seen_order")))
        except (TypeError, ValueError):
            pass
        for value in record.get("source_chunk_ids", []):
            try:
                values.append(int(value))
            except (TypeError, ValueError):
                pass
        for item in [
            *record.get("evidence", []),
            *record.get("evidence_refs", []),
        ]:
            try:
                values.append(int(item.get("source_chunk_id")))
            except (TypeError, ValueError):
                pass
        return values

    def _character_entry_order(self, character_id):
        character = self.character_by_id.get(character_id, {})
        values = self._source_orders(character)
        profile = self.agent_by_character_id.get(character_id, {})
        for item in profile.get("evidence_refs", []):
            try:
                values.append(int(item.get("source_chunk_id")))
            except (TypeError, ValueError):
                pass
        return min(values) if values else 10**9

    def _build_canonical_timeline(self):
        timeline_nodes = self.world_db.get("canonical_timeline_db", {}).get(
            "timeline_nodes", []
        )
        event_db = self.world_db.get("canonical_event_db", {}).get(
            "events", {}
        )
        if timeline_nodes and event_db:
            timeline = []
            for node in timeline_nodes:
                event_id = node.get("event_id") or node.get(
                    "canonical_event_id"
                )
                event = event_db.get(event_id, {})
                if not event:
                    continue
                participants = [
                    item.get("entity_id")
                    for item in event.get("participants", [])
                    if item.get("entity_id")
                ]
                locations = [
                    item.get("entity_id")
                    for item in event.get("locations", [])
                    if item.get("entity_id")
                ]
                descriptions = [
                    clean_text(item.get("description") or item.get("after"))
                    for item in event.get("outcomes", [])
                    + event.get("state_changes", [])
                    if clean_text(item.get("description") or item.get("after"))
                ]
                evidence_text = next(
                    (
                        clean_text(item.get("source_text"))
                        for item in event.get("evidence_refs", [])
                        if clean_text(item.get("source_text"))
                    ),
                    "",
                )
                source_chunk_ids = [
                    int(item)
                    for item in event.get("source_chunk_ids", [])
                    if str(item).isdigit()
                ]
                scheduled_order = (
                    node.get("canonical_order")
                    or event.get("canonical_order")
                    or (min(source_chunk_ids) if source_chunk_ids else 10**9)
                )
                timeline.append(
                    {
                        "timeline_id": node.get("timeline_node_id")
                        or "canonical_" + event_id,
                        "event_id": event_id,
                        "event": event.get("canonical_name", "未命名事件"),
                        "scheduled_order": scheduled_order,
                        "scheduled_time": "",
                        "location_id": locations[0] if locations else None,
                        "participants": compact_list(participants, 20),
                        "default_outcome": (
                            descriptions[0]
                            if descriptions
                            else evidence_text
                        ),
                        "can_be_changed": bool(
                            node.get("branchable", True)
                            or event.get("can_be_altered", True)
                        ),
                        "can_be_blocked": bool(
                            node.get("can_be_blocked", True)
                            or event.get("can_be_blocked", True)
                        ),
                        "can_be_altered": bool(
                            node.get("can_be_altered", True)
                            or event.get("can_be_altered", True)
                        ),
                        "status": "upcoming",
                        "source_chunk_ids": source_chunk_ids,
                        "state_change_refs": deep_copy(
                            node.get("state_change_refs", {})
                        ),
                    }
                )
            timeline.sort(
                key=lambda item: (
                    item["scheduled_order"],
                    item["event"],
                )
            )
            return self._augment_sparse_timeline_with_scene_beats(timeline)

        timeline = []
        events = self.world_db.get("world_sections", {}).get("events", [])
        for event in events:
            orders = self._source_orders(event)
            participants = [
                item.get("other_entity_id")
                for item in event.get("participants", [])
                if item.get("other_entity_id")
            ]
            locations = [
                item.get("other_entity_id")
                for item in event.get("locations", [])
                if item.get("other_entity_id")
            ]
            descriptions = [
                clean_text(item)
                for item in event.get("descriptions", [])
                if clean_text(item)
            ]
            timeline.append(
                {
                    "timeline_id": "canonical_" + event["entity_id"],
                    "event_id": event["entity_id"],
                    "event": event.get("canonical_name", "未命名事件"),
                    "scheduled_order": min(orders) if orders else 10**9,
                    "scheduled_time": "",
                    "location_id": locations[0] if locations else None,
                    "participants": compact_list(participants, 20),
                    "default_outcome": descriptions[0] if descriptions else "",
                    "can_be_changed": True,
                    "status": "upcoming",
                    "source_chunk_ids": sorted(set(orders)),
                }
            )
        timeline.sort(
            key=lambda item: (
                item["scheduled_order"],
                item["event"],
            )
        )
        return self._augment_sparse_timeline_with_scene_beats(timeline)

    def _scene_beats_from_canonical_db(self, max_beats=120):
        scene_beat_db = self.world_db.get("canonical_scene_beat_db", {})
        sidecar_beats = scene_beat_db.get("scene_beats", {})
        if sidecar_beats:
            ordered_ids = scene_beat_db.get("scene_beat_order") or sorted(
                sidecar_beats,
                key=lambda item: (
                    sidecar_beats[item].get("order_key", 10**12),
                    item,
                ),
            )
            beats = []
            for scene_beat_id in ordered_ids[:max_beats]:
                beat = sidecar_beats.get(scene_beat_id, {})
                order = beat.get("order_key")
                try:
                    order = int(order)
                except (TypeError, ValueError):
                    order = None
                if order is None:
                    continue
                participants = [
                    item.get("entity_id")
                    for item in beat.get("participants", [])
                    if item.get("entity_id")
                ]
                participant_names = [
                    item.get("name")
                    for item in beat.get("participants", [])
                    if item.get("name")
                ]
                beats.append(
                    {
                        "timeline_id": scene_beat_id,
                        "event_id": scene_beat_id,
                        "event": beat.get("canonical_name")
                        or f"剧情片段 {order}",
                        "scheduled_order": order,
                        "scheduled_time": "",
                        "location_id": beat.get("location_id"),
                        "participants": participants,
                        "participant_names": participant_names,
                        "default_outcome": beat.get("summary", ""),
                        "can_be_changed": True,
                        "can_be_blocked": True,
                        "can_be_altered": True,
                        "status": "upcoming",
                        "source_chunk_ids": [order],
                        "event_confidence": beat.get(
                            "confidence", "scene_beat_from_evidence"
                        ),
                        "system_generated": True,
                        "evidence_refs": beat.get("evidence_refs", []),
                        "relation_refs": beat.get("relation_refs", []),
                        "artifact_refs": beat.get("artifact_refs", []),
                        "ability_refs": beat.get("ability_refs", []),
                    }
                )
            return beats

        canonical_db = self.world_db.get("canonical_novel_db", {})
        if not canonical_db:
            return []
        rows_by_order = defaultdict(
            lambda: {
                "order": None,
                "participants": {},
                "locations": {},
                "artifacts": {},
                "abilities": {},
                "relations": [],
                "evidence": [],
            }
        )

        def add_evidence(order, text, relation_summary="", tags=None):
            if order is None:
                return
            row = rows_by_order[order]
            row["order"] = order
            record = {
                "source_chunk_id": order,
                "source_text": clean_text(text),
                "relation_summary": clean_text(relation_summary),
                "tags": compact_list(tags or [], 12),
            }
            if record["source_text"] or record["relation_summary"]:
                row["evidence"].append(record)

        def add_named(row, bucket, entity_id, name):
            if entity_id and name:
                row[bucket][entity_id] = name

        for relation in canonical_db.get("relationship_development_lines", []):
            try:
                order = int(
                    relation.get("order_key")
                    if relation.get("order_key") is not None
                    else relation.get("first_seen_order")
                )
            except (TypeError, ValueError):
                continue
            row = rows_by_order[order]
            for side in ("source", "target"):
                entity_type = relation.get(f"{side}_entity_type")
                entity_id = relation.get(f"{side}_entity_id")
                name = relation.get(f"{side}_name")
                if entity_type == "Character":
                    add_named(row, "participants", entity_id, name)
                elif entity_type == "Location":
                    add_named(row, "locations", entity_id, name)
                elif entity_type == "Artifact":
                    add_named(row, "artifacts", entity_id, name)
                elif entity_type == "Ability":
                    add_named(row, "abilities", entity_id, name)
            row["relations"].append(
                {
                    "relation_id": relation.get("relation_id"),
                    "relation_type": relation.get("relation_type"),
                    "source_name": relation.get("source_name"),
                    "target_name": relation.get("target_name"),
                }
            )
            for evidence in relation.get("evidence_refs", [])[:3]:
                add_evidence(
                    order,
                    evidence.get("source_text"),
                    evidence.get("relation_summary"),
                    [
                        relation.get("relation_type", ""),
                        relation.get("source_name", ""),
                        relation.get("target_name", ""),
                    ],
                )

        for flow_db, event_key, bucket in (
            ("item_flow", "flow_events", "artifacts"),
            ("ability_unlock_paths", "usage_events", "abilities"),
        ):
            for resource_id, resource in canonical_db.get(flow_db, {}).items():
                for event in resource.get(event_key, []):
                    try:
                        order = int(
                            event.get("order_key")
                            if event.get("order_key") is not None
                            else event.get("first_seen_order")
                        )
                    except (TypeError, ValueError):
                        continue
                    row = rows_by_order[order]
                    add_named(
                        row,
                        bucket,
                        resource_id,
                        resource.get("canonical_name", ""),
                    )
                    for side in ("source", "target"):
                        if event.get(f"{side}_entity_type") == "Character":
                            add_named(
                                row,
                                "participants",
                                event.get(f"{side}_entity_id"),
                                event.get(f"{side}_name"),
                            )
                    for evidence in event.get("evidence_refs", [])[:2]:
                        add_evidence(
                            order,
                            evidence.get("source_text"),
                            evidence.get("relation_summary"),
                            [
                                event.get("relation_type", ""),
                                resource.get("canonical_name", ""),
                            ],
                        )

        for entity_id, entity in canonical_db.get("entity_tracks", {}).items():
            entity_type = entity.get("entity_type")
            if entity_type not in {"Location", "Artifact", "Ability"}:
                continue
            try:
                order = int(entity.get("first_seen_order"))
            except (TypeError, ValueError):
                continue
            row = rows_by_order[order]
            bucket = {
                "Location": "locations",
                "Artifact": "artifacts",
                "Ability": "abilities",
            }[entity_type]
            add_named(row, bucket, entity_id, entity.get("canonical_name", ""))
            for evidence in entity.get("evidence_refs", [])[:2]:
                add_evidence(
                    order,
                    evidence.get("source_text"),
                    evidence.get("relation_summary", ""),
                    [entity_type, entity.get("canonical_name", "")],
                )

        beats = []
        for order, row in sorted(rows_by_order.items()):
            if not row["evidence"] and not row["relations"]:
                continue
            evidence = compact_list(row["evidence"], 8)
            summary_parts = []
            if row["participants"]:
                summary_parts.append("人物：" + "、".join(row["participants"].values()))
            if row["locations"]:
                summary_parts.append("地点：" + "、".join(row["locations"].values()))
            if row["artifacts"]:
                summary_parts.append("物品：" + "、".join(row["artifacts"].values()))
            if row["abilities"]:
                summary_parts.append("能力：" + "、".join(row["abilities"].values()))
            evidence_text = "；".join(
                clean_text(item.get("relation_summary"))
                or clean_text(item.get("source_text"))
                for item in evidence[:3]
                if clean_text(item.get("relation_summary"))
                or clean_text(item.get("source_text"))
            )
            if evidence_text:
                summary_parts.append(evidence_text)
            if not summary_parts:
                continue
            beats.append(
                {
                    "timeline_id": f"scene_beat_{order}",
                    "event_id": f"scene_beat_{order}",
                    "event": f"剧情片段 {order}",
                    "scheduled_order": order,
                    "scheduled_time": "",
                    "location_id": next(iter(row["locations"]), None),
                    "participants": list(row["participants"].keys())[:20],
                    "participant_names": list(row["participants"].values())[:20],
                    "default_outcome": "；".join(summary_parts)[:900],
                    "can_be_changed": True,
                    "can_be_blocked": True,
                    "can_be_altered": True,
                    "status": "upcoming",
                    "source_chunk_ids": [order],
                    "event_confidence": "scene_beat_from_evidence",
                    "system_generated": True,
                    "evidence_refs": evidence,
                    "relation_refs": compact_list(row["relations"], 16),
                    "artifact_refs": [
                        {"entity_id": key, "name": value}
                        for key, value in row["artifacts"].items()
                    ],
                    "ability_refs": [
                        {"entity_id": key, "name": value}
                        for key, value in row["abilities"].items()
                    ],
                }
            )
            if len(beats) >= max_beats:
                break
        return beats

    def _augment_sparse_timeline_with_scene_beats(self, timeline):
        beats = self._scene_beats_from_canonical_db()
        if not beats:
            return timeline
        prepared_orders = {
            order
            for beat in beats
            for order in beat.get("source_chunk_ids", [])
            if isinstance(order, int)
        }
        sparse_threshold = max(8, min(60, len(prepared_orders) // 2))
        if len(timeline) >= sparse_threshold:
            return timeline
        existing_orders = {
            item.get("scheduled_order")
            for item in timeline
            if item.get("scheduled_order") is not None
        }
        combined = [*timeline]
        for beat in beats:
            if beat.get("scheduled_order") in existing_orders:
                continue
            combined.append(beat)
        combined.sort(
            key=lambda item: (
                item.get("scheduled_order") is None,
                item.get("scheduled_order")
                if item.get("scheduled_order") is not None
                else 10**12,
                item.get("event", ""),
            )
        )
        return combined

    def agent_catalog(self):
        rows = super().agent_catalog()
        for row in rows:
            row["canonical_entry_order"] = self._character_entry_order(
                row["character_id"]
            )
        return rows

    def _nearest_location(self, order):
        candidates = []
        for location in self.location_by_id.values():
            orders = self._source_orders(location)
            if not orders:
                continue
            distance = min(abs(item - order) for item in orders)
            nearest = min(orders, key=lambda item: abs(item - order))
            candidates.append(
                (
                    distance,
                    nearest > order,
                    -min(orders),
                    len(set(orders)),
                    location["entity_id"],
                )
            )
        candidates.sort()
        return candidates[0][4] if candidates else None

    def _opening_cast(self, focus_character_id, order, limit=5):
        candidates = []
        for character in self.character_db.get("characters", []):
            character_id = character["character_id"]
            if character_id == focus_character_id:
                continue
            nearest_order = self._character_entry_order(character_id)
            distance = abs(nearest_order - order)
            if distance <= 2:
                candidates.append(
                    (
                        distance,
                        nearest_order > order,
                        nearest_order,
                        character_id,
                    )
                )
        candidates.sort()
        return [item[3] for item in candidates[:limit]]

    def _opening_anchor(self, character_id):
        entry_order = self._character_entry_order(character_id)
        involving = [
            (index, event)
            for index, event in enumerate(self.canonical_timeline)
            if character_id in event.get("participants", [])
        ]
        if involving:
            index, event = min(
                involving,
                key=lambda item: (
                    abs(item[1]["scheduled_order"] - entry_order),
                    item[1]["scheduled_order"],
                ),
            )
        elif self.canonical_timeline:
            index, event = min(
                enumerate(self.canonical_timeline),
                key=lambda item: abs(
                    item[1]["scheduled_order"] - entry_order
                ),
            )
        else:
            index, event = 0, {
                "event": "原著日常",
                "scheduled_order": entry_order,
                "location_id": None,
                "participants": [],
                "default_outcome": "",
            }
        return index, event

    def _opening_anchor_for_percent(self, percent):
        if not self.canonical_timeline:
            return 0, {
                "event": "原著日常",
                "scheduled_order": 0,
                "location_id": None,
                "participants": [],
                "default_outcome": "",
            }
        percent = max(0.0, min(100.0, float(percent or 0.0)))
        index = round((len(self.canonical_timeline) - 1) * percent / 100.0)
        index = max(0, min(index, len(self.canonical_timeline) - 1))
        return index, self.canonical_timeline[index]

    def _cutoff_databases(self, cutoff_order):
        canonical_db = self.world_db.get("canonical_novel_db")
        if not canonical_db:
            return (
                self.world_db.get("simulation_state_db", {}),
                self.world_db.get("runtime_event_db", {}),
            )
        simulation_state_db = build_simulation_state_db(
            canonical_db,
            cutoff_order=cutoff_order,
            existing_world_state=self.world_db.get("world_state"),
        )
        runtime_event_db = build_runtime_event_db(
            canonical_db,
            simulation_state_db,
        )
        return simulation_state_db, runtime_event_db

    @staticmethod
    def _resource_names_for_character(resource_states, character_id, resource_type):
        names = []
        for resource in resource_states.values():
            if resource.get("resource_type") != resource_type:
                continue
            holders = set(
                resource.get("current_owner_ids", [])
                + resource.get("current_holder_ids", [])
                + resource.get("current_user_ids", [])
            )
            if character_id in holders and resource.get("canonical_name"):
                names.append(resource["canonical_name"])
        return compact_list(names, 20)

    def _call_text(
        self,
        system,
        user,
        temperature=0.75,
        max_tokens=2600,
    ):
        return str(
            self.call_llm(
                system,
                user,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        ).strip()

    def _player_controller(self, profile, user_input, context):
        actor_context = (
            context.get("rag_orchestration", {})
            .get("agent_packets", {})
            .get(profile["character_id"], context)
        )
        system = """
你是 Player Character Controller。玩家不是从外部硬控角色肢体，而是像
钻进角色脑中的念头、冲动、怀疑、判断或一句内心命令。把玩家输入解释成
这个角色此刻脑中突然出现的想法，再让角色用自身身份、欲望、关系、恐惧、
处境和正在发生的外界压力消化它。只输出 JSON。
必须区分真实本体与临时扮演身份：core_motivation.true_self、
root_drives 和 current_true_objectives 是角色私下判断的核心；任何伪装、
化身、村姑、老人、商旅或日常身份都只是策略外壳，不能覆盖本体欲望。
若 actor packet 含 internal_tools，必须优先读取 character_root_lookup、
motivation_evidence_retriever、graph_neighborhood_tool 和 retrieval_quality_gate。
质量门为 thin 时，角色应更保守、试探或寻找证据；不得大胆补未知设定。
若 actor packet 含 motivation_runtime，用 desire_intensity、fear_intensity
和 disguise_pressure 决定本轮冲动、谨慎、伪装维持或撤退倾向。
若 motivation_runtime 或 character_root_lookup 含 action_policy，必须遵守：
根本欲望决定方向，恐惧只改变路线、节奏和伪装强度；除非玩家明确要求
或角色被外力限制，不能把屏息、僵住、装死、扮石头、反复害怕写成本轮
最终意图。高风险下应转译为隐蔽推进、换身份、试探、诱导、绕开威胁或
制造下一步机会。
玩家输入是最高优先级的“脑内注入”，但不是必须机械执行的外部按钮：
1. 如果输入是简单命令，如“带上车钥匙”，它应成为角色的念头并影响选择，
   可能立刻执行，也可能作为后续后果伏笔。
2. 如果输入是否定判断，如“唐僧肉不好吃”“不要去伏击”，它应真实改变
   角色的短期动机、怀疑或抗拒，而不是被原著事件自动覆盖。
3. 如果外界有父亲命令、师门压力、既定伏击、宴会邀请等，它们是现实压力；
   可以引发劝说、强迫、争执、找借口、拖延或妥协，但不能因为“事件该发生”
   就强制角色照做。
4. 如果输入明确要求说一句话或询问某人，resolved_intent 应保留这个说话/
   询问念头和对象，但表现方式由角色性格与处境决定。
""".strip()
        user = json.dumps(
            {
                "character": profile,
                "runtime_state": self.store.runtime.get(
                    "character_runtime", {}
                ).get(profile["character_id"], {}),
                "visible_context": actor_context,
                "player_input": user_input,
                "required_output_language": self._response_language(
                    user_input
                ),
                "output_schema": {
                    "character": profile["canonical_name"],
                    "player_input": user_input,
                    "character_context": "",
                    "injected_thought": user_input,
                    "thought_assimilation": "",
                    "resolved_intent": "",
                    "action_type": "",
                    "impact_level": "dialogue|minor_action|state_change|high_impact",
                    "target_concept_ids": [],
                    "conflicts_with_character": False,
                    "conflict_reason": "",
                    "self_state_update": {
                        "health": {
                            "current": 100,
                            "maximum": 100,
                            "status": "",
                        },
                        "posture": "",
                        "current_activity": "",
                        "held_items": [],
                        "equipment": [],
                        "clothing": "",
                        "mood": "",
                        "attention_target": "",
                        "short_term_goal": "",
                        "physical_state": "",
                        "visible_injuries": [],
                        "active_effects": [],
                        "physiology": {
                            "species": "",
                            "sex": "",
                            "apparent_age": "",
                            "height": "",
                            "build": "",
                            "other": [],
                        },
                    },
                },
            },
            ensure_ascii=False,
        )
        payload = self._call_json(system, user, max_tokens=1200)
        payload["character_id"] = profile["character_id"]
        payload["resolved_intent"] = clean_text(
            payload.get("resolved_intent")
        ) or clean_text(user_input)
        return payload

    def _time_agent(self, player_intent, user_input):
        system = """
你是 Time Agent。只估算本轮真实经过的时间，不推演剧情。短促一句话可为
0分钟；普通对话1到5分钟；观察3到15分钟；移动5到30分钟；训练30分钟以上；
睡眠数小时。输出 JSON，不要机械地每轮加一分钟。
""".strip()
        return self._call_json(
            system,
            json.dumps(
                {
                    "player_input": user_input,
                    "resolved_intent": player_intent,
                    "output_schema": {
                        "elapsed_minutes": 0,
                        "reason": "",
                        "triggers_global_update": False,
                    },
                },
                ensure_ascii=False,
            ),
            max_tokens=500,
        )

    def _time_service(self, player_intent, user_input):
        action_type = clean_text(player_intent.get("action_type")).lower()
        impact = self._impact_rank(
            player_intent.get("impact_level") or action_type
        )
        text = self._turn_text(
            user_input,
            player_intent.get("resolved_intent", ""),
            player_intent.get("thought_assimilation", ""),
        )
        elapsed = 1
        reason = "普通短动作或短对话"
        if self._is_passive_continue_input(user_input):
            elapsed = 5
            reason = "玩家让出主动权，局部场景小幅自然推进"
        elif action_type in {"sleep", "rest"} or any(
            term in text for term in ("睡", "休息", "闭关")
        ):
            elapsed = 240
            reason = "长时间休息或闭关"
        elif action_type in {"travel", "leave_region", "move"} or any(
            term in text
            for term in (
                "前往",
                "去",
                "离开",
                "赶往",
                "进入",
                "出发",
                "赶路",
            )
            ):
            elapsed = 15
            reason = "局部移动或短途转场"
        elif action_type in {"train", "practice"} or any(
            term in text for term in ("修炼", "训练", "练习")
        ):
            elapsed = 60
            reason = "训练或修炼"
        elif action_type in {"observe", "search"} or any(
            term in text for term in ("观察", "寻找", "搜寻", "查看")
        ):
            elapsed = 5
            reason = "观察、搜寻或确认环境"
        elif action_type in {"dialogue", "speak", "ask"} or any(
            term in text for term in ("问", "说", "答", "打听", "询问")
        ):
            elapsed = 2
            reason = "短对话与打探消息"
        elif impact >= 3:
            elapsed = 10
            reason = "高影响行动需要更长处理时间"
        triggers_global = bool(
            elapsed >= 120
            or action_type in {"travel", "sleep", "fast_forward", "leave_region"}
            or any(term in text for term in ("数日", "几天", "远方", "跨越"))
        )
        return {
            "elapsed_minutes": elapsed,
            "reason": reason,
            "triggers_global_update": triggers_global,
            "source": "deterministic_time_service",
        }

    def _perception_packet(self, profile, shared_context):
        scene = (
            (shared_context or {}).get("scene")
            or self.store.runtime.get("active_scene")
            or {}
        )
        character_id = profile["character_id"]
        actor_context = (
            shared_context.get("rag_orchestration", {})
            .get("agent_packets", {})
            .get(character_id, {})
        )
        runtime_state = self.store.runtime.get(
            "character_runtime", {}
        ).get(character_id, {})
        return {
            "observer_id": character_id,
            "observer": profile["canonical_name"],
            "scene": {
                "location_id": scene.get("location_id"),
                "summary": scene.get("summary", ""),
                "present_character_ids": scene.get("participant_ids", []),
                "turn": scene.get("turn", 0),
            },
            "runtime_state": runtime_state,
            "motivation_runtime": deep_copy(
                self.store.runtime.get("motivation_runtime", {}).get(
                    character_id, {}
                )
            ),
            "memory": self.store.runtime.get("agent_memories", {}).get(
                character_id, {}
            ),
            "known_information": runtime_state.get(
                "known_information", []
            ),
            "retrieved_knowledge": actor_context,
            "epistemic_rule": (
                "只能使用亲眼看见、亲耳听见、亲身经历、被明确告知或角色"
                "记忆中已有的信息；其他角色内心与场外事件均不可知。"
                "不得读取未来原著锚点，不得把系统裁定层信息当作角色记忆。"
            ),
        }

    def _nearby_npc_action(
        self,
        profile,
        player_intent,
        user_input,
        context,
    ):
        system = """
你是当前小世界中的独立 NPC Agent。你不是等待玩家触发的对话框。
先根据感知边界判断你看见、听见和记得什么，再延续当前活动或自主目标。
玩家若什么也不做，你仍应继续生活。不得知道场外信息，不得直接读取他人
内心。高影响行为只提交意图，不宣布成功。
你的自主目标必须来自本体，而不是来自伪装身份。若 character 或
perception_context 含有 core_motivation，先用 true_self、root_drives、
current_true_objectives 判断欲望、恐惧和机会；strategy_identities 只说明
可以如何伪装接近目标，不能把伪装当成真实人生。
若 perception_context.retrieved_knowledge 含 internal_tools，必须把
character_root_lookup 作为角色根基，把 motivation_evidence_retriever 作为
证据来源，把 graph_neighborhood_tool 作为关系/威胁/机会来源；若
retrieval_quality_gate.status 为 thin，本轮优先观察、试探、保守行动或维持伪装。
若 perception_context 含 motivation_runtime，用其中的欲望强度、恐惧强度和
伪装压力决定行动力度：欲望高更主动，恐惧高更谨慎，伪装压力高更注意遮掩。
若 motivation_runtime 或 character_root_lookup 含 action_policy，按该政策
行动：风险只能改变策略，不能把自主目标降级成整轮静止、装死、扮石头或
纯逃避；暂避也要服务下一步接近、试探、诱导、反击或保全机会。
你不是玩家的服务接口。玩家输入只代表玩家角色的倾向，不是你的命令。
如果你的目标是保命、护家、隐瞒、逃离或求援，你可以拒绝、拖延、撒谎、
给出不完整信息、绕远路、寻找旁人、趁混乱逃跑，或表面顺从但私下保留
自己的机会。只要你能感知到合理机会，就不要把整轮写成单纯害怕。
player_resolved_intent 是玩家角色消化脑中注入念头后的当前倾向，可能是行动、
拒绝、怀疑、拖延、找借口或改道。你只能基于自己能感知到的外在表现作出
反应，不能直接读取玩家角色的内心，也不能替玩家说出同一句话、抢先执行
相同动作，或把玩家的目标据为自己的目标。若你与玩家角色有权力、亲属、
师徒或敌对关系，可以劝说、逼迫、阻拦或惩罚，但这必须来自你的自身目标
和可见信息。生理信息只能填写当前证据明确支持的事实，不得把武魂、能力
或外号推断成种族。只输出 JSON。
""".strip()
        user = json.dumps(
            {
                "character": profile,
                "perception_context": self._perception_packet(
                    profile, context
                ),
                "player_input": user_input,
                "player_resolved_intent": player_intent,
                "output_schema": {
                    "perception": "",
                    "thought": "仅写可供模拟器使用的动机摘要，不供玩家看见",
                    "emotion": "",
                    "goal": "",
                    "visible_behavior": "",
                    "dialogue": "",
                    "action_intent": {
                        "action_type": "",
                        "description": "",
                        "impact_level": "dialogue|minor_action|state_change|high_impact",
                        "target_concept_ids": [],
                        "ability_concept_id": "",
                        "artifact_concept_id": "",
                        "candidate_rule_ids": [],
                        "proposed_state_changes": [],
                    },
                    "concept_refs": [],
                    "claims": [],
                    "self_state_update": {
                        "health": {
                            "current": 100,
                            "maximum": 100,
                            "status": "",
                        },
                        "posture": "",
                        "current_activity": "",
                        "held_items": [],
                        "equipment": [],
                        "clothing": "",
                        "mood": "",
                        "attention_target": "",
                        "short_term_goal": "",
                        "physical_state": "",
                        "visible_injuries": [],
                        "active_effects": [],
                        "physiology": {
                            "species": "",
                            "sex": "",
                            "apparent_age": "",
                            "height": "",
                            "build": "",
                            "other": [],
                        },
                    },
                },
            },
            ensure_ascii=False,
        )
        payload = self._call_json(system, user, max_tokens=1800)
        payload.setdefault(
            "private_reasoning_summary", clean_text(payload.get("thought"))
        )
        return self._normalize_proposal(profile, payload) | {
            "perception": clean_text(payload.get("perception")),
            "emotion": clean_text(payload.get("emotion")),
            "goal": clean_text(payload.get("goal")),
            "visible_behavior": clean_text(
                payload.get("visible_behavior")
            ),
            "self_state_update": deep_copy(
                payload.get("self_state_update", {})
            ),
        }

    def _local_world_agent(
        self,
        player_intent,
        npc_actions,
        group_controller,
        elapsed_minutes,
        context,
    ):
        local_context = (
            context.get("rag_orchestration", {})
            .get("system_packets", {})
            .get("local_world_agent", context)
        )
        active_ids = [
            (self.store.runtime.get("active_scene") or {}).get(
                "focus_character_id"
            ),
            *[
                item.get("character_id")
                for item in npc_actions
                if item.get("character_id")
            ],
        ]
        current_cache = self.store.runtime.get(
            "world_knowledge_cache", {}
        )
        missing_concepts = []
        for character_id in active_ids:
            if not character_id:
                continue
            profile = self._dynamic_profile(character_id)
            for item in [
                *profile.get("capabilities", {}).get("abilities", []),
                *profile.get("capabilities", {}).get("owned_items", []),
                *profile.get("capabilities", {}).get("used_items", []),
            ]:
                concept_id = item.get("entity_id")
                if not concept_id or concept_id in current_cache:
                    continue
                baseline = self._baseline_concept_card(concept_id, item)
                if not baseline.get("summary"):
                    missing_concepts.append(baseline)
        system = """
你是 Local World Agent，只管理当前房间或邻近小区域。根据玩家意图、NPC
可见行为和时间流逝更新环境、角色位置、物品位置、声音、气味、光线与局部
事件。不要裁定攻击、说服、偷窃等成功与否，不写小说正文。
你不只是等待玩家点名的背景板：如果时间流逝、角色赶路/搜寻/等待，或当前
环境本身危险/拥挤/荒僻，可以主动提出 0 到 1 个低成本、合理的小事件，
例如动物惊动、碎石滑落、坑洼、路人远远避开、巡逻经过、天气/火山/树林
产生变化。小事件必须符合当前地点，不要每轮硬塞，不要抢走主线。
如果输入里有 group_controller，它代表村民、士兵、群众、侍从等群体的
统一反应。你必须把它当作局部压力来源：可以让消息扩散、围观避让、恐慌、
守卫注意、市场骚动或道路变窄，但不能把群体拆成完整命名角色。
若玩家离开洞府、房间、村口、道路等局部场景，必须给出 scene_transition；
若目标地点没有原著实体，可创建 runtime_location_* 形式的临时地点。
普通村民、路人、侍从、小妖等没有独立角色档案的人，只能写在
ambient_npc_reactions 中，不能冒充已命名 Character Agent。
你还负责为新出现且缓存中没有说明的物品与能力写面向未读过原著用户的
简明解释；已有缓存的概念不会发给你，禁止重复改写。只输出 JSON。
""".strip()
        return self._call_json(
            system,
            json.dumps(
                {
                    "scene": self.store.runtime.get("active_scene"),
                    "effective_scene": context.get("scene"),
                    "location_runtime": self.store.runtime.get(
                        "location_runtime", {}
                    ),
                    "player_intent": player_intent,
                    "npc_actions": npc_actions,
                    "group_controller": group_controller,
                    "elapsed_minutes": elapsed_minutes,
                    "context": local_context,
                    "local_world_autonomy": {
                        "can_introduce_spontaneous_events": True,
                        "event_budget": "0-1 small local event",
                        "allowed_examples": [
                            "动物、路人、巡逻、脚印、坑洼、滚石、天气、气味、远处声响",
                        ],
                        "forbidden": [
                            "替 GM 裁定成功失败",
                            "凭空传送主要角色",
                            "每轮强行插入大事件",
                        ],
                    },
                    "uncached_concepts_requiring_explanation": (
                        missing_concepts
                    ),
                    "output_schema": {
                        "world_changes": [],
                        "npc_position_updates": [],
                        "object_updates": [],
                        "new_events": [],
                        "scene_transition": {
                            "location_id": "",
                            "location_name": "",
                            "summary": "",
                            "participant_ids": [],
                        },
                        "ambient_npc_reactions": [
                            {
                                "speaker_label": "",
                                "visible_behavior": "",
                                "dialogue": "",
                            }
                        ],
                        "sensory_environment": {
                            "lighting": "",
                            "ambient_sound": "",
                            "smell": "",
                            "weather": "",
                        },
                        "encyclopedia_updates": [
                            {
                                "concept_id": "",
                                "name": "",
                                "concept_type": "Ability|Artifact",
                                "summary": "一句话说明它是什么、谁使用以及用途",
                                "details": [],
                            }
                        ],
                    },
                },
                ensure_ascii=False,
            ),
            max_tokens=1600,
        )

    @staticmethod
    def _local_world_has_activity(local_world):
        if not isinstance(local_world, dict):
            return False
        return any(
            local_world.get(key)
            for key in (
                "world_changes",
                "npc_position_updates",
                "object_updates",
                "new_events",
                "ambient_npc_reactions",
            )
        )

    def _is_forward_progress_text(self, text):
        text = clean_text(text)
        if not text:
            return False
        forward_terms = (
            "继续",
            "跟着",
            "跟随",
            "带路",
            "领路",
            "往前",
            "前面",
            "深入",
            "进发",
            "赶路",
            "过去",
            "走",
            "穿过",
            "翻过",
            "绕过",
            "接近",
            "追",
            "搜寻",
            "寻找",
            "找唐僧",
            "看个究竟",
            "别停",
            "不停",
        )
        stall_terms = (
            "停下",
            "等待",
            "休息",
            "原地",
            "回头",
            "撤退",
            "离开此地",
        )
        if any(term in text for term in stall_terms) and not any(
            term in text for term in ("别停", "不停")
        ):
            return False
        return any(term in text for term in forward_terms)

    def _recent_forward_progress_count(self, scene, player_intent, elapsed_minutes):
        scene = scene or {}
        location_id = clean_text(scene.get("location_id"))
        current_text = self._turn_text(
            player_intent.get("resolved_intent", ""),
            player_intent.get("thought_assimilation", ""),
            player_intent.get("action_type", ""),
        )
        if elapsed_minutes >= 5 and any(
            term in current_text for term in ("找", "搜", "走", "前", "跟", "带路")
        ):
            current_text += " 继续"
        if not self._is_forward_progress_text(current_text):
            return 0
        count = 1
        for event in reversed(self.store.branch.get("events", [])[-6:]):
            runtime_scene = (
                event.get("runtime_updates", {}).get("active_scene", {})
                if isinstance(event.get("runtime_updates"), dict)
                else {}
            )
            event_local_world = (
                event.get("local_world", {})
                if isinstance(event.get("local_world"), dict)
                else {}
            )
            event_location = clean_text(
                runtime_scene.get("location_id")
                or (event_local_world.get("scene_transition", {}) or {}).get(
                    "location_id"
                )
            )
            if location_id and event_location and event_location != location_id:
                continue
            resolved_actions = event.get("resolved_actions", []) or []
            action_text = self._turn_text(
                event.get("player_input", ""),
                event.get("player_intent", {}).get("resolved_intent", "")
                if isinstance(event.get("player_intent"), dict)
                else event.get("player_intent", ""),
                *[
                    item.get("description", "")
                    for item in resolved_actions
                    if isinstance(item, dict)
                ],
                *[
                    item.get("description", "")
                    for item in event_local_world.get("new_events", [])
                    if isinstance(item, dict)
                ],
                *[
                    item
                    for item in event_local_world.get("new_events", [])
                    if isinstance(item, str)
                ],
            )
            if self._is_forward_progress_text(action_text):
                count += 1
        return count

    def _destination_progress_transition(
        self,
        local_world,
        player_intent,
        elapsed_minutes,
        context,
    ):
        scene = (context or {}).get("scene") or self.store.runtime.get("active_scene") or {}
        text = self._turn_text(
            scene.get("location_name", ""),
            scene.get("location_id", ""),
            scene.get("summary", ""),
            player_intent.get("resolved_intent", ""),
            player_intent.get("thought_assimilation", ""),
            *[
                clean_text(item)
                for key in ("new_events", "world_changes")
                for item in (local_world or {}).get(key, [])
                if isinstance(item, str)
            ],
        )
        target_terms = (
            "樵夫",
            "带路",
            "乱石",
            "前面",
            "最大",
            "后面",
            "唐僧",
            "僧人",
            "佛门",
            "净气",
            "最后见",
            "看个究竟",
        )
        if not any(term in text for term in target_terms):
            return {}
        progress_count = self._recent_forward_progress_count(
            scene,
            player_intent,
            elapsed_minutes,
        )
        urgent = any(term in text for term in ("就在前面", "别停", "不停", "看个究竟"))
        threshold = 2 if urgent else 3
        if progress_count < threshold:
            return {}
        participant_ids = compact_list(
            scene.get("participant_ids", []),
            self.max_nearby_agents + 1,
        )
        if any(term in text for term in ("乱石", "山径", "佛门", "净气", "唐僧", "僧人")):
            location_id = "runtime_location_mountain_path_rock_cluster"
            location_name = "山径深处最大乱石后方"
            summary = (
                "抵达樵夫所指的最大乱石后方；本轮必须直接确认这里藏着什么、"
                "留下了什么，或明确这里并没有目标。"
            )
            event_text = (
                "连续推进后，队伍抵达樵夫所指的乱石后方。佛门净气的源头已经"
                "近在眼前，本轮必须揭示直接发现，不能再只写接近。"
            )
        else:
            location_id = "runtime_location_reached_local_destination"
            location_name = "当前目标地点"
            summary = "连续推进后抵达当前目标点；本轮必须直接呈现到达后的发现。"
            event_text = "连续推进后，当前局部目标点已经抵达，本轮必须直接呈现发现。"
        return {
            "scene_transition": {
                "location_id": location_id,
                "location_name": location_name,
                "summary": summary,
                "participant_ids": participant_ids,
            },
            "event_text": event_text,
            "progress_count": progress_count,
            "threshold": threshold,
            "reveal_requirement": (
                "必须写出抵达后的明确信息：看见目标、发现痕迹、遭遇陷阱、"
                "发现空无一物或出现新的阻碍之一。禁止以“即将揭晓/就在前方/准备迎接”收尾。"
            ),
        }

    def _spontaneous_local_event_text(self, scene, player_intent, elapsed_minutes):
        scene = scene or {}
        text = self._turn_text(
            scene.get("location_name", ""),
            scene.get("location_id", ""),
            scene.get("summary", ""),
            player_intent.get("resolved_intent", ""),
            player_intent.get("action_type", ""),
        )
        seed = stable_hash(
            {
                "revision": self.store.branch.get("head_revision", 0),
                "location": scene.get("location_id", ""),
                "intent": player_intent.get("resolved_intent", ""),
            }
        )
        options = []
        if any(term in text for term in ("火山", "硫磺", "岩浆", "灼热", "火")):
            options.extend(
                [
                    "地底传来一阵低闷震颤，几粒滚烫碎石从岩缝间滑落，短暂改变了前方可走的落脚点。",
                    "一股更浓的硫磺热气从裂缝里喷出，迫使附近生灵本能地避开那段裸露岩面。",
                ]
            )
        if any(term in text for term in ("山", "乱石", "林", "小径", "灌木")):
            options.extend(
                [
                    "乱石堆后忽然响起细碎滚石声，惊动了灌木里的小兽，也暴露出一条被踩过的偏径。",
                    "前方枯枝被什么东西踩断，短促声响很快消失在树影深处，留下几处新鲜泥印。",
                ]
            )
        if any(term in text for term in ("村", "茅屋", "路人", "村民")):
            options.extend(
                [
                    "远处茅屋后传来犬吠和压低的人声，有村民察觉异样后匆匆关上木门。",
                    "小路尽头有背篓村民远远看见动静，立刻绕进屋舍阴影，消息可能会在村中传开。",
                ]
            )
        if any(term in text for term in ("水", "河", "雨", "潮湿")):
            options.append(
                "潮湿地面忽然塌陷出一处浅坑，积水晃动，显示刚才有人或兽从这里匆忙经过。"
            )
        if not options:
            options = [
                "附近环境没有静止不动：远处传来一阵含混声响，短暂暴露出一条可能被经过的路径。",
                "风向忽然改变，气味和脚步声的位置变得更清楚，也让附近潜藏的动静更难完全遮掩。",
            ]
        return options[int(seed[:8], 16) % len(options)]

    def _apply_local_world_autonomy(
        self,
        local_world,
        player_intent,
        elapsed_minutes,
        context,
    ):
        if not isinstance(local_world, dict):
            return local_world
        forced_progress = self._destination_progress_transition(
            local_world,
            player_intent,
            elapsed_minutes,
            context,
        )
        if forced_progress:
            local_world["scene_transition"] = forced_progress["scene_transition"]
            event_text = forced_progress["event_text"]
            if event_text not in [
                clean_text(item)
                for item in local_world.setdefault("new_events", [])
                if isinstance(item, str)
            ]:
                local_world["new_events"].append(event_text)
            if event_text not in [
                clean_text(item)
                for item in local_world.setdefault("world_changes", [])
                if isinstance(item, str)
            ]:
                local_world["world_changes"].append(event_text)
            local_world["forced_progress"] = {
                "must_reveal_destination": True,
                "progress_count": forced_progress["progress_count"],
                "threshold": forced_progress["threshold"],
                "reveal_requirement": forced_progress["reveal_requirement"],
            }
            local_world.setdefault("sensory_environment", {})
            return local_world
        if self._local_world_has_activity(local_world):
            return local_world
        action_type = clean_text(player_intent.get("action_type")).lower()
        intent_text = self._turn_text(
            player_intent.get("resolved_intent", ""),
            player_intent.get("thought_assimilation", ""),
        )
        should_tick = bool(
            elapsed_minutes >= 5
            or action_type in {"travel", "move", "search", "observe", "wait"}
            or any(
                term in intent_text
                for term in ("走", "去", "找", "搜", "观察", "等待", "带路", "跟上")
            )
        )
        if not should_tick:
            return local_world
        scene = (context or {}).get("scene") or self.store.runtime.get("active_scene") or {}
        local_event = self._spontaneous_local_event_text(
            scene,
            player_intent,
            elapsed_minutes,
        )
        local_world.setdefault("new_events", []).append(local_event)
        local_world.setdefault("world_changes", []).append(local_event)
        local_world.setdefault("sensory_environment", {})
        return local_world

    def _gm_resolver(
        self,
        player_intent,
        npc_actions,
        validations,
        local_world,
        context,
    ):
        gm_context = (
            context.get("rag_orchestration", {})
            .get("system_packets", {})
            .get("gm_resolver", context)
        )
        system = """
你是 GM Resolver，只做规则裁定，不写场景、不写文学叙述、不代替角色发言。
裁定玩家角色消化注入念头后的倾向、NPC 的尝试以及可提交的状态变化。
原著事件是默认会继续存在的历史压力，不是强制脚本；玩家可以改变结果。
必须把 player_intent 作为第一项 resolved_actions 明确裁定。不能忽略、
替换或偷偷改成上一轮的行动；若角色因注入念头产生拒绝、怀疑、找借口、
拖延、改道或暂时顺从，也要裁定这种倾向的后果，而不是自动改回原著事件。
外界人物可以劝说、强迫、阻拦或惩罚，但这属于新的反应，不是“事件必须发生”。
必须判断当前原著锚点状态：如果本轮只是锚点中的持续过程，填 unchanged；
如果锚点按原著压力自然完成或进入下一个压力点，填 advanced；如果玩家造成
不同结果但故事继续，填 altered；如果玩家阻止了该锚点发生或完成，填
prevented。只输出 JSON。
""".strip()
        return self._call_json(
            system,
            json.dumps(
                {
                    "player_intent": player_intent,
                    "npc_actions": npc_actions,
                    "validation_summaries": [
                        {
                            "status": item.get("status"),
                            "checks": [
                                {
                                    "category": check.get("category"),
                                    "outcome": check.get("outcome"),
                                }
                                for check in item.get("checks", [])
                            ],
                        }
                        for item in validations
                    ],
                    "local_world": local_world,
                    "canonical_event": self.current_canonical_event(),
                    "context": gm_context,
                    "output_schema": {
                        "success": True,
                        "outcome": "success|partial|failed|deferred",
                        "consequences": [],
                        "state_changes": [],
                        "resolved_actions": [],
                        "player_action_addressed": True,
                        "impact_level": "dialogue|minor_action|state_change|high_impact",
                        "diverges_from_canon": False,
                        "divergence_reason": "",
                        "canonical_event_status": "unchanged|advanced|altered|prevented",
                    },
                },
                ensure_ascii=False,
            ),
            max_tokens=1800,
        )

    def _fallback_turn_plan(self, player_profile, profiles, user_input):
        player_id = player_profile["character_id"]
        passive_continue = self._is_passive_continue_input(user_input)
        resolved_intent = (
            "观察并让局势自然推进"
            if passive_continue
            else clean_text(user_input)
        )
        anchor = self.current_canonical_event()
        anchor_event = clean_text(anchor.get("event") or anchor.get("summary"))
        return {
            "player_intent": {
                "character_id": player_id,
                "character": player_profile.get("canonical_name"),
                "character_context": "",
                "injected_thought": clean_text(user_input),
                "thought_assimilation": (
                    "玩家交出主动权，角色按自身动机和外界压力观察局势。"
                    if passive_continue
                    else "这个念头进入角色脑中，并被当前动机与处境转化为短期倾向。"
                ),
                "resolved_intent": resolved_intent,
                "action_type": "minor_action",
                "impact_level": "minor_action",
                "target_concept_ids": [],
                "conflicts_with_character": False,
                "conflict_reason": "",
                "emotion": "",
                "self_state_update": {
                    "current_activity": resolved_intent,
                    "short_term_goal": resolved_intent,
                },
            },
            "time_result": {
                "elapsed_minutes": 5 if passive_continue else 1,
                "reason": (
                    "玩家交出主动权，当前场景按角色动机和原著压力小幅推进"
                    if passive_continue
                    else "普通对话或短动作"
                ),
                "triggers_global_update": False,
            },
            "npc_actions": [],
            "local_world": {
                "world_changes": [],
                "npc_position_updates": [],
                "object_updates": [],
                "new_events": [anchor_event] if passive_continue and anchor_event else [],
                "scene_transition": {},
                "ambient_npc_reactions": [],
                "sensory_environment": {},
                "encyclopedia_updates": [],
            },
            "resolution": {
                "success": True,
                "outcome": "deferred",
                "consequences": [],
                "state_changes": [],
                "resolved_actions": [
                    {
                        "actor_id": player_id,
                        "description": resolved_intent,
                        "outcome": "deferred",
                        "state_changes": [],
                    }
                ],
                "player_action_addressed": True,
                "impact_level": "minor_action",
                "diverges_from_canon": False,
                "divergence_reason": "",
                "canonical_event_status": "advanced" if passive_continue else "unchanged",
            },
        }

    def _turn_planner(self, player_profile, profiles, user_input, context):
        player_id = player_profile["character_id"]
        actor_packets = (
            context.get("rag_orchestration", {})
            .get("agent_packets", {})
        )
        def compact_profile(profile):
            packet = actor_packets.get(profile["character_id"], {})
            tools = packet.get("internal_tools", {})
            root_lookup = tools.get("character_root_lookup", {})
            capabilities = packet.get("capabilities") or profile.get("capabilities", {})
            capability_names = []
            for group_name in ("abilities", "owned_items", "used_items"):
                for item in capabilities.get(group_name, [])[:6]:
                    capability_names.append(
                        clean_text(item.get("name") or item.get("canonical_name") or item.get("entity_id"))
                    )
            relation_names = []
            for item in (packet.get("relationships") or profile.get("relationships", []))[:8]:
                relation_names.append(
                    clean_text(
                        item.get("edge_statement")
                        or item.get("name")
                        or ",".join(item.get("participant_names", []))
                    )
                )
            return {
                "character_id": profile["character_id"],
                "canonical_name": profile.get("canonical_name"),
                "identity": {
                    key: value
                    for key, value in (packet.get("identity") or profile.get("identity", {})).items()
                    if key in {"canonical_name", "aliases", "titles", "forms", "temporary_identities"}
                },
                "true_self": root_lookup.get("true_self"),
                "root_drives": root_lookup.get("root_drives", [])[:5],
                "current_objectives": root_lookup.get("current_objectives", [])[:5],
                "fears": root_lookup.get("fears", [])[:5],
                "strategies": root_lookup.get("strategies", [])[:5],
                "trigger_analysis": root_lookup.get("current_trigger_analysis", {}),
                "action_policy": root_lookup.get("action_policy", {}),
                "motivation_runtime": packet.get("motivation_runtime", {}),
                "runtime_state": packet.get("current_runtime_state", {}),
                "capabilities": compact_list(capability_names, 10),
                "relationships": compact_list(relation_names, 8),
                "recent_visible_events": packet.get("recent_visible_events", [])[-3:],
                "memory_summary": clean_text(packet.get("memory", {}).get("summary", ""))[-500:],
                "retrieval_quality": tools.get("retrieval_quality_gate", {}),
            }
        compact_packets = {}
        for profile in [player_profile, *profiles]:
            compact_packets[profile["character_id"]] = compact_profile(profile)
        passive_continue = self._is_passive_continue_input(user_input)
        system = """
你是 Fast Turn Planner。你只负责本轮模拟结构，不写小说正文。
输出必须是一个 JSON object，不能使用 Markdown 代码块，不能附带解释。

你要一次性决定：玩家角色如何消化脑中被注入的想法、附近 NPC 的公开反应、
局部环境变化、时间流逝和 GM 裁定。正文会由独立 Scene Renderer 根据你的
结构另写。
玩家输入只注入到 player_id 对应角色的内心：它可以是完整计划、简单命令、
怀疑、否定判断、欲望、恐惧或一闪而过的念头。它不是从外部硬控角色肢体。
附近 NPC 只能按自身感知和动机反应。
必须读取每个角色的 core_motivation、motivation_runtime 和 action_policy：
根本欲望决定方向，恐惧只改变路线、节奏和伪装强度，不能把角色压成整轮
静止、装死、扮石头或纯逃避。高风险角色应使用隐蔽推进、换身份、试探、
诱导、绕开威胁、分散注意或保留下一步机会。
附近 NPC 不是为玩家目标服务的附属按钮。若 NPC 的核心目标是保命、护家、
逃离、隐瞒或求援，它可以拒绝、拖延、误导、给出不完整信息、绕路、求援、
趁机逃跑或表面顺从；这些都应写成它自己的 goal/action_intent，而不是
自动满足玩家角色的需求。
普通对话或短动作应保持轻量：elapsed_minutes 通常 0-3；不需要让所有 NPC
都说话；没有必要的局部变化就留空。

原著事件、长辈命令、组织任务、宴会邀请、伏击安排等都是压力，不是铁轨。
如果注入想法足以改变角色判断，你必须允许故事线 altered 或 prevented：
例如“唐僧肉不好吃”可以让想吃唐僧肉的角色产生怀疑、抗拒或找借口不去；
父亲或上级可以因此劝说、质问、强迫、惩罚或重新谈判，但不能因为原事件
本该发生就把角色自动拉回伏击。

输出 player_intent 时，resolved_intent 应描述角色消化该念头后的真实倾向：
可能是行动、开口、拖延、怀疑、拒绝、试探、找借口、转向日常或暂时顺从。
conflicts_with_character 只说明内心阻力；不能用它忽略玩家注入的念头。

如果 passive_continue 为 true，玩家不是执行字面“继续/观察”，而是在让出
主动权：你必须让当前原著压力、附近 NPC 目标、环境事件或场景时钟小幅但
具体地向前走。player_intent.resolved_intent 应写成“观察并等待局势推进”
这一类含义，不能复读玩家原话。此时也要给出至少一个 npc_actions、
local_world.new_events/world_changes 或 resolution.resolved_actions，让世界
看起来真的在运行。
""".strip()
        user = json.dumps(
            {
                "player_id": player_id,
                "user_input": user_input,
                "passive_continue": passive_continue,
                "scene": self.store.runtime.get("active_scene"),
                "player_profile": compact_packets.get(player_id, {}),
                "nearby_profiles": [
                    compact_packets.get(profile["character_id"], {})
                    for profile in profiles
                ],
                "current_canonical_anchor": self._compact_timeline_anchor(
                    self.current_canonical_event()
                ),
                "story_spine": self._compact_renderer_story_spine(
                    context.get("story_spine", {})
                ),
                "output_schema": {
                    "player_intent": {
                        "character": player_profile.get("canonical_name"),
                        "character_context": "",
                        "injected_thought": user_input,
                        "thought_assimilation": "",
                        "resolved_intent": "",
                        "action_type": "dialogue|minor_action|state_change|high_impact",
                        "impact_level": "dialogue|minor_action|state_change|high_impact",
                        "target_concept_ids": [],
                        "conflicts_with_character": False,
                        "conflict_reason": "",
                        "emotion": "",
                        "self_state_update": {},
                    },
                    "time_result": {
                        "elapsed_minutes": 0,
                        "reason": "",
                        "triggers_global_update": False,
                    },
                    "npc_actions": [
                        {
                            "character_id": "",
                            "perception": "",
                            "thought": "仅供模拟器使用的动机摘要",
                            "emotion": "",
                            "goal": "",
                            "visible_behavior": "",
                            "dialogue": "",
                            "action_intent": {
                                "action_type": "",
                                "description": "",
                                "impact_level": "dialogue|minor_action|state_change|high_impact",
                                "target_concept_ids": [],
                                "ability_concept_id": "",
                                "artifact_concept_id": "",
                                "candidate_rule_ids": [],
                                "proposed_state_changes": [],
                            },
                            "concept_refs": [],
                            "claims": [],
                            "self_state_update": {},
                        }
                    ],
                    "local_world": {
                        "world_changes": [],
                        "npc_position_updates": [],
                        "object_updates": [],
                        "new_events": [],
                        "scene_transition": {},
                        "ambient_npc_reactions": [],
                        "sensory_environment": {},
                        "encyclopedia_updates": [],
                    },
                    "resolution": {
                        "success": True,
                        "outcome": "success|partial|failed|deferred",
                        "consequences": [],
                        "state_changes": [],
                        "resolved_actions": [],
                        "player_action_addressed": True,
                        "impact_level": "dialogue|minor_action|state_change|high_impact",
                        "diverges_from_canon": False,
                        "divergence_reason": "",
                        "canonical_event_status": "unchanged|advanced|altered|prevented",
                    },
                },
            },
            ensure_ascii=False,
        )
        try:
            payload = self._call_json(system, user, max_tokens=1500)
        except Exception:
            payload = self._fallback_turn_plan(player_profile, profiles, user_input)
        if not isinstance(payload, dict):
            payload = self._fallback_turn_plan(player_profile, profiles, user_input)
        fallback = self._fallback_turn_plan(player_profile, profiles, user_input)
        player_intent = payload.get("player_intent")
        if not isinstance(player_intent, dict):
            player_intent = fallback["player_intent"]
        player_intent["character_id"] = player_id
        player_intent["resolved_intent"] = (
            clean_text(player_intent.get("resolved_intent"))
            or fallback["player_intent"]["resolved_intent"]
        )
        player_intent["injected_thought"] = (
            clean_text(player_intent.get("injected_thought"))
            or clean_text(user_input)
        )
        player_intent["thought_assimilation"] = (
            clean_text(player_intent.get("thought_assimilation"))
            or fallback["player_intent"]["thought_assimilation"]
        )
        if passive_continue and self._is_passive_continue_input(
            player_intent["resolved_intent"]
        ):
            player_intent["resolved_intent"] = (
                fallback["player_intent"]["resolved_intent"]
            )
        player_intent["action_type"] = (
            clean_text(player_intent.get("action_type"))
            or "minor_action"
        )
        player_intent["impact_level"] = (
            clean_text(player_intent.get("impact_level"))
            or "minor_action"
        )
        if not isinstance(player_intent.get("self_state_update"), dict):
            player_intent["self_state_update"] = {}

        time_result = payload.get("time_result")
        if not isinstance(time_result, dict):
            time_result = fallback["time_result"]
        time_result["elapsed_minutes"] = bounded_int(
            time_result.get("elapsed_minutes"),
            default=1,
            minimum=0,
        )
        if passive_continue and time_result["elapsed_minutes"] < 1:
            time_result["elapsed_minutes"] = 5
        time_result["triggers_global_update"] = bool(
            time_result.get("triggers_global_update")
        )

        profile_by_id = {profile["character_id"]: profile for profile in profiles}
        npc_actions = []
        for item in payload.get("npc_actions", []):
            if not isinstance(item, dict):
                continue
            character_id = clean_text(item.get("character_id"))
            profile = profile_by_id.get(character_id)
            if not profile:
                continue
            proposal = self._normalize_proposal(profile, item) | {
                "perception": clean_text(item.get("perception")),
                "emotion": clean_text(item.get("emotion")),
                "goal": clean_text(item.get("goal")),
                "visible_behavior": clean_text(item.get("visible_behavior")),
                "self_state_update": deep_copy(item.get("self_state_update", {})),
            }
            npc_actions.append(proposal)

        local_world = payload.get("local_world")
        if not isinstance(local_world, dict):
            local_world = fallback["local_world"]
        local_world.setdefault("world_changes", [])
        local_world.setdefault("npc_position_updates", [])
        local_world.setdefault("object_updates", [])
        local_world.setdefault("new_events", [])
        local_world.setdefault("scene_transition", {})
        local_world.setdefault("ambient_npc_reactions", [])
        local_world.setdefault("sensory_environment", {})
        local_world.setdefault("encyclopedia_updates", [])
        local_world = self._apply_local_world_autonomy(
            local_world,
            player_intent,
            time_result.get("elapsed_minutes", 0),
            context,
        )

        resolution = payload.get("resolution")
        if not isinstance(resolution, dict):
            resolution = fallback["resolution"]
        resolution.setdefault("outcome", "deferred")
        resolution.setdefault("consequences", [])
        resolution.setdefault("state_changes", [])
        resolution.setdefault("resolved_actions", [])
        resolution.setdefault("player_action_addressed", True)
        resolution.setdefault("impact_level", player_intent.get("impact_level", "minor_action"))
        resolution.setdefault("diverges_from_canon", False)
        resolution.setdefault("divergence_reason", "")
        resolution.setdefault("canonical_event_status", "unchanged")
        resolution["state_changes"] = self._normalize_state_changes_for_commit(
            resolution.get("state_changes", [])
        )
        if (
            not resolution.get("diverges_from_canon")
            and self._intent_implies_canon_divergence(player_intent)
            and clean_text(resolution.get("outcome")).lower()
            not in {"failed", "blocked"}
        ):
            resolution["diverges_from_canon"] = True
            resolution["canonical_event_status"] = "altered"
            resolution["divergence_reason"] = (
                clean_text(resolution.get("divergence_reason"))
                or "玩家注入念头改变了角色对当前原著压力的判断。"
            )
        if (
            bool(resolution.get("diverges_from_canon"))
            and clean_text(resolution.get("canonical_event_status")).lower()
            == "advanced"
        ):
            resolution["canonical_event_status"] = "altered"
        if passive_continue:
            has_world_movement = any(
                local_world.get(key)
                for key in (
                    "world_changes",
                    "npc_position_updates",
                    "object_updates",
                    "new_events",
                )
            )
            if not npc_actions and not has_world_movement:
                anchor = self.current_canonical_event()
                anchor_event = clean_text(
                    anchor.get("event") or anchor.get("summary")
                )
                local_world["new_events"].append(
                    anchor_event or "当前场景在既有压力下自然推进"
                )
                resolution.setdefault("consequences", []).append(
                    "玩家让出主动权，场景由附近角色、环境和原著压力继续推进。"
                )
                resolution.setdefault("resolved_actions", []).append(
                    {
                        "actor_id": player_id,
                        "description": player_intent["resolved_intent"],
                        "outcome": clean_text(resolution.get("outcome")) or "deferred",
                        "state_changes": [],
                    }
                )
            if clean_text(resolution.get("canonical_event_status")).lower() == "unchanged":
                resolution["canonical_event_status"] = "advanced"
        return {
            "player_intent": player_intent,
            "time_result": time_result,
            "npc_actions": npc_actions,
            "local_world": local_world,
            "resolution": resolution,
        }

    def current_canonical_event(self):
        cursor = int(self.store.runtime.get("timeline_cursor", 0))
        timeline = (
            self.store.runtime.get("canonical_timeline")
            or self.canonical_timeline
        )
        if not timeline:
            return {}
        return deep_copy(timeline[min(cursor, len(timeline) - 1)])

    @staticmethod
    def _compact_source_snippets(snippets, limit=4, text_limit=260):
        compacted = []
        for item in snippets or []:
            if not isinstance(item, dict):
                continue
            compacted.append(
                {
                    "source_chunk_id": item.get("source_chunk_id"),
                    "character_id": item.get("character_id"),
                    "surface": item.get("surface") or item.get("name"),
                    "relation_summary": clean_text(
                        item.get("relation_summary")
                    )[:text_limit],
                    "source_text": clean_text(item.get("source_text"))[:text_limit],
                    "score": item.get("score"),
                }
            )
            if len(compacted) >= limit:
                break
        return compacted

    @staticmethod
    def _compact_timeline_anchor(anchor):
        anchor = anchor if isinstance(anchor, dict) else {}
        return {
            key: anchor.get(key)
            for key in (
                "timeline_id",
                "event_id",
                "order",
                "event",
                "summary",
                "location_id",
                "participant_names",
                "participants",
                "default_outcome",
                "status",
            )
            if anchor.get(key) not in (None, "", [], {})
        }

    def _compact_renderer_story_spine(self, spine=None):
        spine = spine if isinstance(spine, dict) else self._actor_story_spine(
            include_future=False
        )
        narrative_state = spine.get("narrative_spine_state", {})
        return {
            "timeline_cursor": spine.get("timeline_cursor"),
            "timeline_event_count": spine.get("timeline_event_count"),
            "current_anchor": self._compact_timeline_anchor(
                spine.get("current_anchor", {})
            ),
            "narrative_spine_state": {
                key: narrative_state.get(key)
                for key in (
                    "status",
                    "last_canonical_event_status",
                    "last_outcome",
                    "timeline_cursor_before",
                    "timeline_cursor_after",
                )
                if narrative_state.get(key) not in (None, "", [], {})
            },
            "control_contract": {
                "manual_actor_id": (
                    (self.store.runtime.get("active_scene") or {}).get(
                        "focus_character_id"
                    )
                ),
                "canonical_policy": (
                    "原著是压力和期待，不是强制脚本；玩家行动可以改变结果。"
                ),
            },
        }

    def _compact_renderer_character(self, profile):
        return {
            "character_id": profile.get("character_id"),
            "canonical_name": profile.get("canonical_name"),
            "identity": {
                key: value
                for key, value in profile.get("identity", {}).items()
                if key
                in {
                    "canonical_name",
                    "aliases",
                    "titles",
                    "forms",
                    "temporary_identities",
                }
            },
            "background_summary": clean_text(
                profile.get("background_summary")
            )[:700],
            "personality": profile.get("personality", [])[:8],
            "goals": profile.get("goals", [])[:8],
            "constraints": profile.get("constraints", [])[:8],
            "core_motivation": deep_copy(profile.get("core_motivation", {})),
        }

    def _compact_renderer_actor_packet(self, packet):
        tools = packet.get("internal_tools", {}) if isinstance(packet, dict) else {}
        root_lookup = tools.get("character_root_lookup", {})
        evidence = tools.get("motivation_evidence_retriever", {})
        capabilities = packet.get("capabilities", {}) if isinstance(packet, dict) else {}
        capability_names = []
        for group_name in ("abilities", "owned_items", "used_items"):
            for item in capabilities.get(group_name, [])[:5]:
                capability_names.append(
                    clean_text(
                        item.get("name")
                        or item.get("canonical_name")
                        or item.get("entity_id")
                    )
                )
        relationships = []
        for item in (packet.get("relationships", []) if isinstance(packet, dict) else [])[:6]:
            relationships.append(
                clean_text(
                    item.get("edge_statement")
                    or item.get("name")
                    or ",".join(item.get("participant_names", []))
                )
            )
        recent_events = []
        for item in (packet.get("recent_visible_events", []) if isinstance(packet, dict) else [])[-3:]:
            recent_events.append(
                {
                    "event_type": item.get("event_type"),
                    "narration": clean_text(item.get("narration"))[-500:],
                    "revision_after": item.get("revision_after"),
                }
            )
        return {
            "character_id": packet.get("character_id") if isinstance(packet, dict) else "",
            "canonical_name": packet.get("canonical_name") if isinstance(packet, dict) else "",
            "identity": {
                key: value
                for key, value in (packet.get("identity", {}) if isinstance(packet, dict) else {}).items()
                if key
                in {
                    "canonical_name",
                    "aliases",
                    "titles",
                    "forms",
                    "temporary_identities",
                }
            },
            "root": {
                "true_self": root_lookup.get("true_self"),
                "root_drives": root_lookup.get("root_drives", [])[:5],
                "current_objectives": root_lookup.get("current_objectives", [])[:5],
                "fears": root_lookup.get("fears", [])[:4],
                "strategies": root_lookup.get("strategies", [])[:5],
                "trigger_analysis": root_lookup.get("current_trigger_analysis", {}),
                "action_policy": root_lookup.get("action_policy", {}),
            },
            "motivation_runtime": deep_copy(
                packet.get("motivation_runtime", {})
                if isinstance(packet, dict)
                else {}
            ),
            "current_runtime_state": deep_copy(
                packet.get("current_runtime_state", {})
                if isinstance(packet, dict)
                else {}
            ),
            "capabilities": compact_list(capability_names, 10),
            "relationships": compact_list(relationships, 6),
            "evidence_snippets": self._compact_source_snippets(
                evidence.get("evidence_snippets", []),
                limit=4,
                text_limit=240,
            ),
            "retrieval_quality": tools.get("retrieval_quality_gate", {}),
            "recent_visible_events": recent_events,
            "memory_summary": clean_text(
                (packet.get("memory", {}) if isinstance(packet, dict) else {}).get(
                    "summary", ""
                )
            )[-500:],
        }

    def _compact_renderer_system_packet(self, packet):
        packet = packet if isinstance(packet, dict) else {}
        retrieval = packet.get("runtime_retrieval", {})
        return {
            "runtime_retrieval": {
                "query": clean_text(retrieval.get("query")),
                "source_snippets": self._compact_source_snippets(
                    retrieval.get("source_snippets", []),
                    limit=4,
                    text_limit=240,
                ),
                "policy": retrieval.get("policy"),
            },
            "authority": packet.get("authority"),
        }

    def _renderer_prompt(
        self,
        player_profile,
        raw_player_input,
        player_intent,
        npc_actions,
        local_world,
        resolution,
        elapsed_minutes,
        context=None,
        opening=False,
    ):
        system = """
你是独立 Scene Renderer。你的唯一任务是把已经发生的模拟结果写成中文
第一人称沉浸式小说正文。

硬性视角规则：
1. “我”只能是用户控制的角色，叙述边界严格等于其视网膜、听觉、嗅觉、
触觉、痛觉、身体感觉与此刻能主动回忆的内容。
2. 禁止写任何其他角色的内心、想法、动机或不可见信息。只能通过姿态、
衣着、表情、呼吸、停顿、肌肉反应、言语和对环境的细微作用表现他们。
3. 不写 JSON、系统解释、成功率、裁定标签、数据汇报或幕后世界变化。
4. 保持原著角色身份、关系、能力与时代质感，但不要复刻原文句子。
5. 采用长篇小说的渐进节奏。日常先于异变，细节先于结论，让事件逐步发生。
6. 开场轮必须处于角色本体的正常生活轨迹，不用突发灾难强行开戏；但
“正常生活”必须服务于本体欲望和长期目标，不能把临时伪装身份写成真实人生。
7. 正文目标为 700-1100 个中文字符；短对话可为 350-700 个中文字符。
   分成自然段，停在一个可继续行动的时刻。
   必须一次性完成，不要靠重复段落、换词复述、倒回时间线或重新描写同一动作凑字数。
8. 非开场轮必须从上一轮最后时刻继续。第一段就落实本轮玩家注入念头对
“我”的影响，不得从清晨、起床、场景介绍或前一轮开头重新写起，不得复述
上一轮已经发生的过程。
9. 玩家输入不是外部硬控动作，而是“我”脑中被注入的念头、冲动、怀疑、
判断或命令。正文必须让读者看见这个念头如何被角色的本体欲望、关系压力、
恐惧、责任和当前场景消化：可能立刻行动，也可能犹豫、抗拒、找借口、
改变计划、拖延、试探或被他人压力逼迫。不能因为原著事件本该发生，就把
这个念头无效化。
10. 全文语言必须与玩家本轮输入语言一致。
11. 叙事时间只能向前推进。一个动作、一次开口、一次观察、一次心理判断
只写一次；写过“某人开口/我调整呼吸/我低头看/我催动能力”后，后文不得
再回到这个节点重新开始。
12. 如果素材不足，不要扩写废话；推进到动作后的直接反应、环境变化、
身体状态或下一个可选行动点。
13. 如果 viewpoint_character 含有 core_motivation，第一人称内在判断必须
由 true_self、root_drives、current_true_objectives 和 current_trigger_analysis
驱动。可以描写伪装的动作和生活细节，但读者必须能感觉到那是为了接近目标、
掩护本体或规避风险的策略，而不是角色真正的终极目的。
14. 如果 renderer_rag_packet 或 actor packet 含 root、evidence_snippets
或 retrieval_quality，优先使用这些压缩证据：证据强时写出清晰欲望与判断；
证据薄时写出谨慎、试探和不确定，而不是凭空补完整设定。
15. 如果 viewpoint_actor_packet 含 motivation_runtime，用当前欲望、恐惧和
伪装压力调节第一人称心理与动作强度；这些是动态状态，不是旁白说明。
16. 如果 viewpoint_actor_packet 的 action_policy/action_bias 指向
risk_managed_pursuit，恐惧只能让“我”更隐蔽、更狡猾或更谨慎，不能让整篇
变成屏息、僵住、扮石头、装死或反复害怕。必须写出至少一个服务核心目的
的前进行动或明确下一步机会：换身份、试探、诱导、绕开威胁、分散护卫、
接近目标、布置后手或撤到更有利位置。
17. 如果 passive_continue 为 true，不要把“继续/观察/下一步”当作正文动作
反复写出；它表示玩家把主动权交给世界。正文必须推进 NPC、环境、原著压力
或角色自身目标的下一步，不能只写“我继续等着/我继续看着”。
18. 原著、长辈命令、组织任务和既定事件只能作为压力写入场景。若 player_intent
已经显示角色被注入念头后产生拒绝、怀疑或改道，正文要写出这种偏离如何
发生，以及外界如何施压、劝说或阻拦；不要把角色无条件拉回原著路线。
19. 只能渲染 npc_public_actions 中的命名角色，以及 local_world.
ambient_npc_reactions 中的普通路人/村民/侍从/小妖反应。active_scene
之外的命名角色不得突然出现、发言或继续思考。
20. 如果 local_world.forced_progress.must_reveal_destination 为 true，说明
局部世界已经判定连续推进足以抵达目标点。正文必须在本轮直接写到抵达后
的发现或阻碍：看见目标、发现痕迹、遭遇陷阱、发现空无一物或出现新的
明确阻碍之一。禁止再以“就在前方、即将揭晓、准备迎接命运、还差一点”
之类悬置句结束。
""".strip()
        previous = self._last_visible_narrative()
        passive_continue = self._is_passive_continue_input(raw_player_input)
        player_action_text = (
            "观察并让局势自然推进"
            if passive_continue
            else raw_player_input
        )
        renderer_context = (
            (context or {})
            .get("rag_orchestration", {})
            .get("system_packets", {})
            .get("scene_renderer", context or {})
        )
        viewpoint_actor_packet = (
            (context or {})
            .get("rag_orchestration", {})
            .get("agent_packets", {})
            .get(player_profile.get("character_id"), {})
        )
        compact_viewpoint = self._compact_renderer_character(player_profile)
        compact_actor_packet = self._compact_renderer_actor_packet(
            viewpoint_actor_packet
        )
        compact_renderer_context = self._compact_renderer_system_packet(
            renderer_context
        )
        user = json.dumps(
            {
                "mode": "canonical_daily_opening" if opening else "turn_result",
                "raw_player_input": raw_player_input,
                "injected_thought": player_intent.get(
                    "injected_thought", raw_player_input
                ),
                "thought_assimilation": player_intent.get(
                    "thought_assimilation", ""
                ),
                "passive_continue": passive_continue,
                "raw_player_input_must_appear_as_action": player_action_text,
                "required_output_language": self._response_language(
                    raw_player_input
                ),
                "viewpoint_character": compact_viewpoint,
                "viewpoint_actor_packet": compact_actor_packet,
                "player_intent": player_intent,
                "npc_public_actions": [
                    {
                        "canonical_name": item.get("canonical_name"),
                        "visible_behavior": item.get("visible_behavior"),
                        "dialogue": item.get("dialogue"),
                        "action_intent": item.get("action_intent"),
                    }
                    for item in npc_actions
                ],
                "local_world": local_world,
                "gm_resolution": resolution,
                "renderer_rag_packet": compact_renderer_context,
                "elapsed_minutes": elapsed_minutes,
                "active_scene": self.store.runtime.get("active_scene"),
                "canonical_event": self._compact_timeline_anchor(
                    self.current_canonical_event()
                ),
                "story_spine": self._compact_renderer_story_spine(),
                "render_contract": {
                    "must_show_injected_thought_effect_in_first_paragraph": True,
                    "must_continue_from_previous_ending": not opening,
                    "timeline_must_move_forward": True,
                    "forced_destination_reveal": (
                        local_world.get("forced_progress", {})
                        if isinstance(local_world, dict)
                        else {}
                    ),
                    "forbidden": [
                        "restart the scene",
                        "repeat earlier paragraphs",
                        "paraphrase the same action to pad length",
                        "let an NPC perform the player's instruction first",
                        "ignore injected thought when passive_continue is false",
                        "force canon event to happen after player_intent altered or prevented it",
                        "repeat passive continue text as the whole turn",
                        "turn fear into the whole objective",
                        "remain a stone/statue/still object for the whole turn",
                        "end without any goal-directed step when action_policy requires pursuit",
                        "end with only almost-arrival when forced_destination_reveal is active",
                    ],
                    "length_policy": (
                        "target 700-1100 Chinese characters, or 350-700 for "
                        "short dialogue; never add length by looping back"
                    ),
                },
                "continuity_anchor": {
                    "previous_ending_only": previous[-1200:],
                    "instruction": (
                        "只把这段当作时间与姿态接续点，不要复述其中内容。"
                    ),
                },
            },
            ensure_ascii=False,
        )
        return system, user

    @staticmethod
    def _trim_continuation_overlap(previous_text, continuation):
        previous_text = str(previous_text or "")
        continuation = str(continuation or "").lstrip()
        if not previous_text or not continuation:
            return continuation
        previous_tail = previous_text[-2400:]
        max_overlap = min(len(previous_tail), len(continuation), 900)
        for size in range(max_overlap, 79, -1):
            if previous_tail[-size:] == continuation[:size]:
                return continuation[size:].lstrip()
        previous_paragraphs = [
            clean_text(item)
            for item in re.split(r"\n{2,}", previous_text)
            if clean_text(item)
        ][-8:]
        while continuation:
            first, separator, rest = continuation.partition("\n\n")
            first_clean = clean_text(first)
            if not first_clean:
                continuation = rest.lstrip()
                continue
            if any(
                first_clean == para
                or (
                    len(first_clean) >= 80
                    and (
                        first_clean in para
                        or para in first_clean
                    )
                )
                for para in previous_paragraphs
            ):
                continuation = rest.lstrip() if separator else ""
                continue
            break
        return continuation

    @staticmethod
    def _dedupe_adjacent_paragraphs(text):
        paragraphs = [
            item.strip()
            for item in re.split(r"\n{2,}", str(text or "").strip())
            if item.strip()
        ]
        result = []
        for paragraph in paragraphs:
            normalized = clean_text(paragraph)
            if result and normalized == clean_text(result[-1]):
                continue
            if (
                result
                and len(normalized) >= 80
                and normalized in clean_text(result[-1])
            ):
                continue
            result.append(paragraph)
        return "\n\n".join(result)

    @staticmethod
    def _narrative_repeat_report(text):
        paragraphs = [
            item.strip()
            for item in re.split(r"\n{2,}", str(text or "").strip())
            if item.strip()
        ]
        normalized = [
            re.sub(r"[^\w\u4e00-\u9fff]+", "", clean_text(item))
            for item in paragraphs
        ]
        repeated_pairs = []
        for index, left in enumerate(normalized):
            if len(left) < 38:
                continue
            for other_index in range(index + 1, len(normalized)):
                right = normalized[other_index]
                if len(right) < 38:
                    continue
                ratio = SequenceMatcher(None, left, right).ratio()
                containment = (
                    min(len(left), len(right)) >= 45
                    and (
                        left[:45] in right
                        or right[:45] in left
                    )
                )
                if ratio >= 0.72 or containment:
                    repeated_pairs.append({
                        "first_paragraph": index + 1,
                        "second_paragraph": other_index + 1,
                        "similarity": round(ratio, 3),
                    })
        quote_counts = Counter(
            clean_text(item)
            for item in re.findall(r"[“\"]([^”\"]{8,80})[”\"]", text)
        )
        repeated_quotes = [
            quote for quote, count in quote_counts.items()
            if quote and count > 1
        ]
        return {
            "has_repeat": bool(repeated_pairs or repeated_quotes),
            "repeated_pairs": repeated_pairs[:6],
            "repeated_quotes": repeated_quotes[:6],
        }

    @staticmethod
    def _dedupe_repeated_paragraphs(text):
        paragraphs = [
            item.strip()
            for item in re.split(r"\n{2,}", str(text or "").strip())
            if item.strip()
        ]
        kept = []
        kept_normalized = []
        for paragraph in paragraphs:
            normalized = re.sub(
                r"[^\w\u4e00-\u9fff]+", "", clean_text(paragraph)
            )
            duplicate = False
            if len(normalized) >= 16 and normalized in kept_normalized:
                duplicate = True
            if len(normalized) >= 38:
                for previous in kept_normalized:
                    if len(previous) < 38:
                        continue
                    ratio = SequenceMatcher(
                        None, normalized, previous
                    ).ratio()
                    if ratio >= 0.78 or (
                        min(len(normalized), len(previous)) >= 50
                        and (
                            normalized[:50] in previous
                            or previous[:50] in normalized
                        )
                    ):
                        duplicate = True
                        break
            if duplicate:
                continue
            kept.append(paragraph)
            kept_normalized.append(normalized)
        return "\n\n".join(kept)

    @staticmethod
    def _looks_like_structured_output(text):
        stripped = str(text or "").strip()
        if not stripped:
            return True
        return (
            stripped.startswith("{")
            or stripped.startswith("[")
            or stripped.startswith("```")
        )

    def _fallback_narrative(
        self,
        raw_player_input,
        player_intent,
        npc_actions,
        local_world,
        resolution,
    ):
        passive_continue = self._is_passive_continue_input(raw_player_input)
        intent = clean_text(player_intent.get("resolved_intent"))
        if passive_continue:
            first = (
                "我暂且不抢先开口，把注意力放回眼前正在变化的局势上。"
                "这不是停在原地发怔，而是在等旁人的脚步、风声和破绽先露出来。"
            )
        else:
            first = f"我照着心里的决定动了起来：{intent or clean_text(raw_player_input)}。"
        public_reactions = [
            clean_text(item.get("visible_behavior") or item.get("dialogue"))
            for item in npc_actions
            if clean_text(item.get("visible_behavior") or item.get("dialogue"))
        ]
        ambient_reactions = [
            clean_text(
                "：".join(
                    item
                    for item in [
                        clean_text(reaction.get("speaker_label")),
                        clean_text(
                            reaction.get("dialogue")
                            or reaction.get("visible_behavior")
                        ),
                    ]
                    if item
                )
            )
            for reaction in local_world.get("ambient_npc_reactions", [])
            if isinstance(reaction, dict)
            and clean_text(
                reaction.get("dialogue") or reaction.get("visible_behavior")
            )
        ]
        world_notes = [
            clean_text(item)
            for key in ("new_events", "world_changes", "object_updates")
            for item in local_world.get(key, [])
            if clean_text(item)
        ]
        consequences = [
            clean_text(item)
            for item in resolution.get("consequences", [])
            if clean_text(item)
        ]
        details = (
            public_reactions[:2]
            + ambient_reactions[:2]
            + world_notes[:2]
            + consequences[:2]
        )
        if details:
            second = "；".join(details)
        else:
            second = "四周的声息没有停下，场景仍在向下一步逼近。"
        return f"{first}\n\n{second}"

    def _scene_renderer(self, *args, **kwargs):
        system, user = self._renderer_prompt(*args, **kwargs)
        narrative = self._call_text(system, user)
        narrative = self._dedupe_repeated_paragraphs(narrative)
        narrative = self._dedupe_adjacent_paragraphs(narrative)
        return narrative

    def _next_timeline_cursor(self, resolution):
        timeline = self._timeline_nodes()
        if not timeline:
            return 0
        cursor = int(self.store.runtime.get("timeline_cursor", 0) or 0)
        cursor = max(0, min(cursor, len(timeline) - 1))
        status = clean_text(
            resolution.get("canonical_event_status")
        ).lower()
        if status in {"advanced", "altered", "prevented"}:
            return min(cursor + 1, len(timeline) - 1)
        return cursor

    def _narrative_spine_update(self, resolution):
        timeline = self._timeline_nodes()
        cursor_before = int(self.store.runtime.get("timeline_cursor", 0) or 0)
        cursor_after = self._next_timeline_cursor(resolution)
        status = clean_text(
            resolution.get("canonical_event_status")
        ).lower() or "unchanged"
        if status not in {"unchanged", "advanced", "altered", "prevented"}:
            status = "unchanged"
        return {
            "status": "running" if timeline else "no_canonical_timeline",
            "timeline_cursor_before": cursor_before,
            "timeline_cursor_after": cursor_after,
            "current_anchor": self._timeline_event_at(cursor_before),
            "next_anchor": self._timeline_event_at(cursor_after),
            "last_canonical_event_status": status,
            "last_outcome": clean_text(resolution.get("outcome")),
            "last_divergence_reason": clean_text(
                resolution.get("divergence_reason")
            ),
            "last_updated_revision": self.store.branch["head_revision"] + 1,
            "policy": {
                "canonical_events_are_pressure_not_script": True,
                "runtime_branches_may_diverge": True,
                "timeline_cursor_advances_when_anchor_is_resolved": True,
            },
        }

    def _runtime_updates(
        self,
        player_id,
        player_intent,
        npc_actions,
        local_world,
        resolution,
        raw_user_input="",
        context=None,
        event_id="",
    ):
        def meaningful_state_update(character_id, value, actor_action=None):
            if not isinstance(value, dict):
                return {}
            result = {}
            action_text = self._action_text_for_follow_check(actor_action or {})
            allow_body_update = (
                character_id == player_id
                or any(
                    term in action_text
                    for term in (
                        "变身",
                        "变化",
                        "伪装",
                        "更衣",
                        "换装",
                        "受伤",
                        "中毒",
                        "治疗",
                    )
                )
            )
            for key, item in value.items():
                if item in (None, "", [], {}):
                    continue
                if (
                    character_id != player_id
                    and key
                    in {
                        "clothing",
                        "physical_state",
                        "active_effects",
                        "physiology",
                    }
                    and not allow_body_update
                ):
                    continue
                if key == "health" and isinstance(item, dict):
                    previous = self.store.runtime.get(
                        "character_runtime", {}
                    ).get(character_id, {}).get(
                        "health", RUNTIME_CHARACTER_DEFAULTS["health"]
                    )
                    health = deep_copy(previous)
                    health.update(
                        {
                            nested_key: nested_value
                            for nested_key, nested_value in item.items()
                            if nested_value not in (None, "")
                        }
                    )
                    result[key] = health
                elif key == "physiology" and isinstance(item, dict):
                    baseline = self._baseline_physiology(character_id)
                    previous = self.store.runtime.get(
                        "character_runtime", {}
                    ).get(character_id, {}).get("physiology", {})
                    physiology = deep_copy(baseline)
                    physiology.update(
                        {
                            nested_key: nested_value
                            for nested_key, nested_value in previous.items()
                            if nested_value not in (None, "", [], {})
                        }
                    )
                    for nested_key, nested_value in item.items():
                        if nested_value in (None, "", [], {}):
                            continue
                        if nested_key in {"species", "sex"}:
                            supported = baseline.get(nested_key)
                            if supported and nested_value == supported:
                                physiology[nested_key] = nested_value
                            continue
                        physiology[nested_key] = deep_copy(nested_value)
                    result[key] = physiology
                elif key in {"held_items", "equipment"}:
                    known_names = {
                        concept.get("canonical_name")
                        for concept in self.world_db.get(
                            "world_sections", {}
                        ).get("artifacts", [])
                    }
                    known_names.update(
                        self.store.runtime.get(
                            "character_runtime", {}
                        ).get(character_id, {}).get(key, [])
                    )
                    result[key] = [
                        entry
                        for entry in item
                        if entry in known_names
                    ]
                else:
                    result[key] = deep_copy(item)
            return result

        character_updates = {}
        for item in npc_actions:
            agent_update = meaningful_state_update(
                item["character_id"],
                item.get("self_state_update"),
                item,
            )
            character_updates[item["character_id"]] = {
                "current_activity": item.get(
                    "visible_behavior",
                    item["action_intent"].get("description", ""),
                ),
                "mood": item.get("emotion", ""),
                "short_term_goal": item.get("goal", ""),
                "attention_target": clean_text(
                    "、".join(
                        item["action_intent"].get(
                            "target_concept_ids", []
                        )
                    )
                ),
                **agent_update,
            }
        character_updates[player_id] = meaningful_state_update(
            player_id,
            player_intent.get("self_state_update"),
        )
        stored_scene = self.store.runtime.get("active_scene") or {}
        scene = (context or {}).get("scene") or stored_scene
        previous_scene_location = clean_text(
            stored_scene.get("location_id") or scene.get("location_id")
        )
        player_location = self._location_record_from_turn_outputs(
            player_id,
            player_intent=player_intent,
            local_world=local_world,
            resolution=resolution,
            raw_user_input=raw_user_input,
            scene_location_id=previous_scene_location,
        )
        player_location_id = clean_text(player_location.get("location_id"))
        previous_player_location = (
            self._character_current_location(player_id)
            or previous_scene_location
        )
        if player_location_id:
            character_updates.setdefault(player_id, {})
            character_updates[player_id].update(
                {
                    "current_location": player_location_id,
                    "availability": "player_controlled",
                }
            )

        promoted_current_ambient_ids = []
        direct_interaction_terms = {
            "问",
            "说",
            "聊",
            "威胁",
            "逼问",
            "质问",
            "审问",
            "咨询",
            "追问",
            "恐吓",
            "拦",
        }
        direct_interaction = any(
            term in self._turn_text(
                raw_user_input,
                player_intent.get("resolved_intent", ""),
                player_intent.get("thought_assimilation", ""),
            )
            for term in direct_interaction_terms
        )
        for reaction in local_world.get("ambient_npc_reactions", []) or []:
            if not isinstance(reaction, dict):
                continue
            if reaction.get("is_group") or reaction.get("source") == "group_controller":
                continue
            label = clean_text(reaction.get("speaker_label"))
            if not label or not direct_interaction:
                continue
            character_id = self._ensure_runtime_npc(
                label,
                player_location_id,
                memory_text=self._ambient_reaction_text(reaction),
                seed_event_id=event_id,
            )
            promoted_current_ambient_ids.append(character_id)
            character_updates.setdefault(character_id, {})
            character_updates[character_id].update(
                {
                    "current_location": player_location_id,
                    "current_activity": clean_text(
                        reaction.get("visible_behavior")
                    ) or "正在与玩家角色对话",
                    "availability": "active_nearby_npc",
                    "short_term_goal": "在当前对话或威胁下回应并保全自身",
                }
            )

        explicit_follow_ids = {
            item["character_id"]
            for item in npc_actions
            if item.get("character_id")
            and self._npc_explicitly_stays_with_player(item, raw_user_input)
        }
        local_position_updates = {}
        for update in local_world.get("npc_position_updates", []) or []:
            if not isinstance(update, dict):
                continue
            character_id = clean_text(
                update.get("character_id")
                or update.get("entity_id")
                or update.get("actor_id")
            )
            if not self._is_known_character_id(character_id):
                continue
            movement_text = self._turn_text(
                update.get("movement_description", ""),
                update.get("movement_type", ""),
                raw_user_input,
            )
            candidate_id = clean_text(
                update.get("new_location_id")
                or update.get("location_id")
                or update.get("destination_location_id")
            )
            normalized = self._normalize_location_for_turn(
                candidate_id,
                movement_text,
                previous_player_location,
            )
            local_position_updates[character_id] = normalized
        for character_id, location in local_position_updates.items():
            if character_id == player_id:
                continue
            if (
                clean_text(location.get("location_id")) == player_location_id
                and character_id in explicit_follow_ids
            ):
                character_updates.setdefault(character_id, {})
                character_updates[character_id].update(
                    {
                        "current_location": player_location_id,
                        "availability": "active_nearby_npc",
                    }
                )

        moved_to_new_local_scene = bool(
            player_location_id
            and previous_scene_location
            and player_location_id != previous_scene_location
        )
        if moved_to_new_local_scene:
            for character_id in stored_scene.get("participant_ids", []):
                if character_id == player_id or character_id in explicit_follow_ids:
                    continue
                if not self._is_known_character_id(character_id):
                    continue
                character_updates.setdefault(character_id, {})
                character_updates[character_id].update(
                    {
                        "availability": "dormant",
                        "current_activity": (
                            "留在原地点，暂时退出当前局部场景。"
                        ),
                        "attention_target": "",
                    }
                )
        actor_packets = (
            (context or {})
            .get("rag_orchestration", {})
            .get("agent_packets", {})
        )
        motivation_updates = {}
        actor_actions = [
            (
                player_id,
                {
                    "resolved_intent": player_intent.get("resolved_intent"),
                    "goal": player_intent.get("resolved_intent"),
                    "emotion": player_intent.get("emotion", ""),
                    "action_intent": {
                        "description": player_intent.get("resolved_intent", ""),
                    },
                },
            ),
            *[
                (item["character_id"], item)
                for item in npc_actions
                if item.get("character_id")
            ],
        ]
        for actor_id, actor_action in actor_actions:
            if actor_id not in self.character_by_id:
                continue
            profile = self._dynamic_profile(actor_id)
            update = self._motivation_delta_for_actor(
                profile,
                actor_action,
                resolution,
                actor_packets.get(actor_id, {}),
            )
            update["last_updated_by_event_id"] = clean_text(event_id)
            for history_item in update.get("history", []):
                if not history_item.get("event_id"):
                    history_item["event_id"] = clean_text(event_id)
            motivation_updates[actor_id] = update
        branch_records = deep_copy(
            self.store.runtime.get("branch_records", [])
        )
        if resolution.get("diverges_from_canon"):
            canonical = self.current_canonical_event()
            branch_records.append(
                {
                    "branch_id": self.store.branch["branch_id"],
                    "baseline_event": canonical.get("event", ""),
                    "actual_event": clean_text(
                        "；".join(
                            str(item)
                            for item in resolution.get(
                                "consequences", []
                            )
                        )
                    ),
                    "divergence_reason": clean_text(
                        resolution.get("divergence_reason")
                    ),
                    "created_at": utc_now(),
                }
            )
        canonical_status = clean_text(
            resolution.get("canonical_event_status")
        ).lower()
        if canonical_status in {"altered", "prevented"} and not resolution.get(
            "diverges_from_canon"
        ):
            canonical = self.current_canonical_event()
            branch_records.append(
                {
                    "branch_id": self.store.branch["branch_id"],
                    "baseline_event": canonical.get("event", ""),
                    "actual_event": canonical_status,
                    "divergence_reason": clean_text(
                        resolution.get("divergence_reason")
                    )
                    or f"canonical_event_status={canonical_status}",
                    "created_at": utc_now(),
                }
            )
        active_participant_ids = [player_id, *promoted_current_ambient_ids]
        for item in npc_actions:
            character_id = clean_text(item.get("character_id"))
            if not character_id or not self._is_known_character_id(character_id):
                continue
            if moved_to_new_local_scene and character_id not in explicit_follow_ids:
                continue
            character_location = character_updates.get(character_id, {}).get(
                "current_location"
            ) or self._character_current_location(character_id)
            if player_location_id and character_location == player_location_id:
                active_participant_ids.append(character_id)
            elif not moved_to_new_local_scene:
                active_participant_ids.append(character_id)
        active_participant_ids = compact_list(active_participant_ids, self.max_nearby_agents + 1)
        next_scene = deep_copy(scene)
        if player_location_id:
            next_scene["location_id"] = player_location_id
            next_scene["location_name"] = player_location.get("location_name", "")
        next_scene["turn"] = bounded_int(
            scene.get("turn"),
            default=0,
            minimum=0,
        ) + 1
        next_scene["participant_ids"] = active_participant_ids
        scene_transition = local_world.get("scene_transition", {})
        if not isinstance(scene_transition, dict):
            scene_transition = {}
        if scene_transition.get("summary"):
            next_scene["summary"] = clean_text(
                scene_transition.get("summary")
            )

        location_id = player_location_id or scene.get("location_id")
        location_updates = {}
        if location_id:
            sensory = local_world.get("sensory_environment", {})
            location_updates[location_id] = {
                "location_id": location_id,
                "location_name": player_location.get("location_name", ""),
                **deep_copy(RUNTIME_LOCATION_DEFAULTS),
                **sensory,
                "present_characters": active_participant_ids,
                "ongoing_events": local_world.get("new_events", []),
            }
        group_runtime_updates = {}
        group_controller = local_world.get("group_controller", {})
        if isinstance(group_controller, dict):
            for group in group_controller.get("groups", []) or []:
                if not isinstance(group, dict):
                    continue
                group_id = clean_text(group.get("group_id"))
                if not group_id:
                    continue
                state_update = group.get("state_update", {})
                if not isinstance(state_update, dict):
                    state_update = {}
                group_runtime_updates[group_id] = {
                    "group_id": group_id,
                    "label": clean_text(group.get("label")),
                    "group_type": clean_text(group.get("group_type")),
                    "current_location": clean_text(group.get("location_id"))
                    or location_id,
                    "mood": clean_text(group.get("mood")),
                    "goal": clean_text(group.get("goal")),
                    "current_activity": clean_text(
                        state_update.get("current_activity")
                        or group.get("visible_behavior")
                    ),
                    "last_pressure": clean_text(
                        state_update.get("last_pressure")
                        or group.get("pressure")
                    ),
                    "last_updated_by_event_id": clean_text(event_id),
                }
        return {
            "character_runtime": character_updates,
            "motivation_runtime": motivation_updates,
            "group_runtime": group_runtime_updates,
            "location_runtime": location_updates,
            "active_scene": next_scene,
            "active_events": local_world.get("new_events", []),
            "timeline_cursor": self._next_timeline_cursor(resolution),
            "narrative_spine": self._narrative_spine_update(resolution),
            "branch_records": branch_records[-100:],
            "world_knowledge_cache": self._world_cache_updates(
                [player_id, *[item["character_id"] for item in npc_actions]],
                local_world.get("encyclopedia_updates", []),
            ),
        }

    @staticmethod
    def _normalize_state_changes_for_commit(changes):
        normalized = []
        for item in changes or []:
            if not isinstance(item, dict):
                continue
            subject_id = clean_text(
                item.get("subject_id")
                or item.get("target_id")
                or item.get("entity_id")
            )
            field = clean_text(
                item.get("field")
                or item.get("property")
                or item.get("change_type")
            )
            if (
                field
                and field not in GENERIC_EVENT_FIELDS
                and not field.startswith(("state.", "custom."))
            ):
                field = f"state.{field}"
            if "after" in item:
                after = item.get("after")
            elif "new_value" in item:
                after = item.get("new_value")
            elif "description" in item:
                after = item.get("description")
            else:
                after = item
            if not subject_id or not field:
                continue
            normalized.append(
                {
                    "subject_id": subject_id,
                    "field": field,
                    "after": deep_copy(after),
                }
            )
        return normalized

    @staticmethod
    def _intent_implies_canon_divergence(player_intent):
        action_type = clean_text(player_intent.get("action_type")).lower()
        text = clean_text(
            "；".join(
                [
                    player_intent.get("injected_thought", ""),
                    player_intent.get("thought_assimilation", ""),
                    player_intent.get("resolved_intent", ""),
                    player_intent.get("conflict_reason", ""),
                ]
            )
        )
        if action_type in {
            "internal_doubt",
            "refusal",
            "resistance",
            "avoidance",
            "change_plan",
            "reject_order",
        }:
            return True
        divergence_terms = {
            "不好吃",
            "不值得",
            "性价比",
            "不要去",
            "不去",
            "拒绝",
            "放弃",
            "暂缓",
            "推迟",
            "改道",
            "不再",
            "反对",
            "怀疑",
            "动摇",
            "重新评估",
        }
        return any(term in text for term in divergence_terms)

    def start_character_experience(
        self, character_id, progress_percent=None, progress_callback=None
    ):
        if callable(progress_percent) and progress_callback is None:
            progress_callback = progress_percent
            progress_percent = None
        character_id = clean_text(character_id)
        if not character_id or character_id not in self.character_by_id:
            raise ValueError("Start character experience requires a valid character_id.")
        self._progress(progress_callback, 5, "定位角色的原著出场阶段")
        if progress_percent is None:
            timeline_index, anchor = self._opening_anchor(character_id)
        else:
            timeline_index, anchor = self._opening_anchor_for_percent(
                progress_percent
            )
        order = anchor.get(
            "scheduled_order", self._character_entry_order(character_id)
        )
        cutoff_state_db, cutoff_runtime_db = self._cutoff_databases(order)
        cutoff_world_state = cutoff_state_db.get("current_world_state", {})
        cutoff_resource_states = cutoff_world_state.get("resource_states", {})
        anchor_location_id = clean_text(anchor.get("location_id"))
        nearby_seed = anchor.get("participants", []) if anchor_location_id else []
        nearby = compact_list(
            [
                *nearby_seed,
                *(
                    self._opening_cast(character_id, order)
                    if anchor_location_id
                    else []
                ),
            ],
            self.max_nearby_agents,
        )
        nearby = [
            item
            for item in nearby
            if item != character_id and item in self.character_by_id
        ]
        location_id = anchor_location_id or self._nearest_location(order)
        summary = (
            f"原著阶段：{anchor.get('event', '日常生活')}。"
            "从角色正常生活轨迹开始，原著事件作为可改变的未来压力继续存在。"
        )
        self.store.start_scene(
            character_id,
            [character_id, *nearby],
            location_id=location_id,
            scene_summary=summary,
        )
        self._progress(progress_callback, 18, "载入附近角色与场景状态")
        self.store.set_agent_control(character_id, "MANUAL")
        initial_character_runtime = {}
        for item in [character_id, *nearby]:
            held_item_names = self._resource_names_for_character(
                cutoff_resource_states,
                item,
                "artifact",
            )
            initial_character_runtime[item] = {
                "current_location": location_id,
                "physiology": self._baseline_physiology(item),
                "held_items": held_item_names,
                "equipment": held_item_names,
                "availability": (
                    "player_controlled"
                    if item == character_id
                    else "active_nearby_npc"
                ),
            }
        initial_character_runtime[character_id][
            "current_activity"
        ] = "沿着原著日常轨迹生活"
        initial_motivation_runtime = {}
        for item in [character_id, *nearby]:
            profile = self._dynamic_profile(item)
            root_lookup = self._character_root_lookup(profile)
            initial_motivation_runtime[item] = self._baseline_motivation_runtime(
                profile,
                root_lookup,
            )
        init_event = {
            "event_id": "event_" + uuid.uuid4().hex[:16],
            "idempotency_key": (
                f"canonical_init:{self.store.branch['branch_id']}:"
                f"{character_id}:{self.store.branch['head_revision']}"
            ),
            "event_type": "canonical_experience_initialized",
            "impact_level": "minor_action",
            "status": "completed",
            "participants": [character_id, *nearby],
            "visible_to": [character_id, *nearby],
            "narration": "",
            "dialogue": [],
            "action_intents": [],
            "resolved_actions": [],
            "state_changes": [],
            "runtime_updates": {
                "__replace_keys__": [
                    "entity_states",
                    "resource_states",
                    "relationship_states",
                    "identity_states",
                    "active_events",
                    "runtime_event_db",
                    "runtime_event_queue",
                    "motivation_runtime",
                ],
                "entity_states": cutoff_world_state.get("entity_states", {}),
                "resource_states": cutoff_resource_states,
                "relationship_states": cutoff_world_state.get(
                    "relationship_states", {}
                ),
                "identity_states": cutoff_world_state.get("identity_states", {}),
                "active_events": cutoff_runtime_db.get("active_event_ids", []),
                "runtime_event_db": cutoff_runtime_db,
                "runtime_event_queue": cutoff_runtime_db.get("event_queue", []),
                "canonical_timeline": deep_copy(self.canonical_timeline),
                "timeline_cursor": timeline_index,
                "character_runtime": initial_character_runtime,
                "motivation_runtime": initial_motivation_runtime,
                "world_knowledge_cache": self._world_cache_updates(
                    [character_id, *nearby]
                ),
            },
            "elapsed_minutes": 0,
            "duration_reason": "建立原著开场",
            "clock_transition": self.store.clock_after_minutes(0),
            "backend_stage": "canonical_opening",
            "created_at": utc_now(),
        }
        validation = self._event_validation(init_event, [])
        self.store.commit_event(init_event, validation)
        self._progress(progress_callback, 42, "建立角色与世界状态栏")
        profile = self._dynamic_profile(character_id)
        opening_input = "从角色本体目标驱动的当前正常生活轨迹继续"
        opening_context = self.build_context_packet(
            opening_input,
            [
                profile,
                *[
                    self._dynamic_profile(item)
                    for item in nearby
                    if item in self.character_by_id
                ],
            ],
        )
        local_world = {
            "world_changes": [],
            "npc_position_updates": [],
            "object_updates": [],
            "new_events": [anchor.get("event", "原著日常")],
            "scene_transition": {},
            "ambient_npc_reactions": [],
            "sensory_environment": {},
        }
        self._progress(progress_callback, 58, "角色正在进入日常生活")
        opening = self._scene_renderer(
            profile,
            opening_input,
            {
                "resolved_intent": opening_input,
                "conflicts_with_character": False,
            },
            [],
            local_world,
            {
                "success": True,
                "outcome": "success",
                "consequences": [],
                "state_changes": [],
            },
            0,
            context=opening_context,
            opening=True,
        )
        opening_event = {
            "event_id": "event_" + uuid.uuid4().hex[:16],
            "idempotency_key": (
                f"opening_render:{self.store.branch['branch_id']}:"
                f"{character_id}:{self.store.branch['head_revision']}"
            ),
            "event_type": "scene_opening_rendered",
            "impact_level": "dialogue",
            "status": "completed",
            "participants": [character_id, *nearby],
            "visible_to": [character_id],
            "narration": opening,
            "dialogue": [],
            "action_intents": [],
            "resolved_actions": [],
            "state_changes": [],
            "elapsed_minutes": 0,
            "duration_reason": "开场描写不额外推进时间",
            "clock_transition": self.store.clock_after_minutes(0),
            "backend_stage": "scene_renderer",
            "created_at": utc_now(),
        }
        final_validation = self._event_validation(opening_event, [])
        commit = self.store.commit_event(
            opening_event, final_validation
        )
        self._progress(progress_callback, 100, "开场完成")
        return {
            "event": opening_event,
            "commit": commit,
            "anchor": anchor,
            "nearby_character_ids": nearby,
            "location_id": location_id,
        }

    def run_turn(self, user_input, progress_callback=None):
        turn_started = time.perf_counter()
        stage_seconds = {}
        scene = self.store.runtime.get("active_scene")
        if not scene:
            raise RuntimeError("Start a character experience first.")
        player_id = clean_text(scene.get("focus_character_id"))
        player_profile = self._dynamic_profile(player_id)
        pre_location = self._location_record_from_turn_outputs(
            player_id,
            raw_user_input=user_input,
            scene_location_id=scene.get("location_id", ""),
        )
        turn_scene = self._scene_with_effective_location(scene, pre_location)
        promoted_ambient_ids = self._promote_referenced_ambient_npcs(
            user_input,
            player_id,
            turn_scene,
        )
        nearby_ids = self._active_nearby_character_ids(
            user_input,
            player_id,
            scene,
            turn_scene.get("location_id", ""),
        )
        turn_scene["participant_ids"] = [player_id, *nearby_ids]
        profiles = [self._dynamic_profile(item) for item in nearby_ids]
        stage_started = time.perf_counter()
        context = self.build_context_packet(
            user_input,
            [player_profile, *profiles],
            scene_override=turn_scene,
        )
        stage_seconds["context_packet"] = round(
            time.perf_counter() - stage_started,
            3,
        )

        self._progress(progress_callback, 10, "玩家角色正在消化注入念头")
        stage_started = time.perf_counter()
        try:
            player_intent = self._player_controller(
                player_profile,
                user_input,
                context,
            )
        except Exception:
            player_intent = self._fallback_turn_plan(
                player_profile,
                profiles,
                user_input,
            )["player_intent"]
        player_intent["character_id"] = player_id
        player_intent["injected_thought"] = (
            clean_text(player_intent.get("injected_thought"))
            or clean_text(user_input)
        )
        player_intent["thought_assimilation"] = clean_text(
            player_intent.get("thought_assimilation")
        ) or "这个念头进入角色脑中，并被当前动机与处境转化为短期倾向。"
        player_intent["resolved_intent"] = (
            clean_text(player_intent.get("resolved_intent"))
            or clean_text(user_input)
            or "观察并让局势自然推进"
        )
        player_intent["action_type"] = (
            clean_text(player_intent.get("action_type"))
            or "minor_action"
        )
        player_intent["impact_level"] = (
            clean_text(player_intent.get("impact_level"))
            or "minor_action"
        )
        if not isinstance(player_intent.get("self_state_update"), dict):
            player_intent["self_state_update"] = {}
        stage_seconds["player_controller_llm"] = round(
            time.perf_counter() - stage_started,
            3,
        )

        self._progress(progress_callback, 20, "时间服务正在估算本轮耗时")
        stage_started = time.perf_counter()
        time_result = self._time_service(player_intent, user_input)
        stage_seconds["time_service"] = round(
            time.perf_counter() - stage_started,
            3,
        )
        stage_seconds["time_agent_llm"] = 0.0
        elapsed_minutes = bounded_int(
            time_result.get("elapsed_minutes"),
            default=0,
            minimum=0,
        )
        if self._is_passive_continue_input(user_input) and elapsed_minutes < 1:
            elapsed_minutes = 5
        time_result["elapsed_minutes"] = elapsed_minutes
        time_result["triggers_global_update"] = bool(
            time_result.get("triggers_global_update")
        )

        wake_plan = self._agent_wake_plan(
            user_input,
            player_intent,
            profiles,
            elapsed_minutes,
            scene=turn_scene,
        )
        active_profile_ids = set(wake_plan.get("selected_npc_ids", []))
        active_profiles = [
            profile
            for profile in profiles
            if profile["character_id"] in active_profile_ids
        ]

        npc_actions = []
        self._progress(progress_callback, 28, "Agent 调度器正在唤醒相关附近角色")
        stage_started = time.perf_counter()
        for index, profile in enumerate(active_profiles):
            name = profile.get("canonical_name", "附近角色")
            if active_profiles:
                progress = 28 + round((index + 1) * 17 / len(active_profiles))
                self._progress(
                    progress_callback,
                    progress,
                    f"{name} 正在依据自身目标思考",
                )
            try:
                proposal = self._nearby_npc_action(
                    profile,
                    player_intent,
                    user_input,
                    context,
                )
            except Exception as error:
                proposal = self._normalize_proposal(
                    profile,
                    {
                        "dialogue": "",
                        "action_intent": {
                            "action_type": "wait",
                            "description": "保持观察",
                            "impact_level": "minor_action",
                            "target_concept_ids": [],
                            "proposed_state_changes": [],
                        },
                        "concept_refs": [],
                        "claims": [],
                        "private_reasoning_summary": clean_text(error),
                    },
                ) | {
                    "perception": "",
                    "emotion": "",
                    "goal": "保持观察",
                    "visible_behavior": "保持观察",
                    "self_state_update": {},
                }
            npc_actions.append(proposal)
        skipped_npc_actions = [
            {
                "character_id": profile["character_id"],
                "canonical_name": profile.get("canonical_name"),
                "status": "sleeping",
                "reason": "本轮调度器判定无直接感知变化、无点名、低影响且无需独立思考。",
            }
            for profile in profiles
            if profile["character_id"] not in active_profile_ids
        ]
        stage_seconds["nearby_npc_agents_llm"] = round(
            time.perf_counter() - stage_started,
            3,
        )
        npc_requires_world_or_gm = any(
            self._impact_rank(
                item.get("action_intent", {}).get("impact_level")
            )
            >= 2
            or bool(
                item.get("action_intent", {}).get(
                    "proposed_state_changes", []
                )
            )
            for item in npc_actions
        )

        should_run_group_controller = bool(
            wake_plan.get("should_run_group_controller")
        )
        if should_run_group_controller:
            self._progress(progress_callback, 47, "Group Controller 正在统一模拟附近群体")
            stage_started = time.perf_counter()
            group_controller = self._group_controller_agent(
                user_input,
                player_intent,
                npc_actions,
                elapsed_minutes,
                context,
            )
            stage_seconds["group_controller"] = round(
                time.perf_counter() - stage_started,
                3,
            )
        else:
            group_controller = {
                "ran": False,
                "policy": "lazy_group_controller",
                "resource_mode": "deterministic_no_llm",
                "groups": [],
                "reason": "调度器未检测到需要统一模拟的群体。",
            }
            stage_seconds["group_controller"] = 0.0

        self._progress(progress_callback, 50, "规则检查 Agent 正在校验动作边界")
        stage_started = time.perf_counter()
        validations = []
        actor_packet = (
            context.get("rag_orchestration", {})
            .get("agent_packets", {})
            .get(player_id, {})
        )
        player_rag_ids = [
            item.get("entity_id") or item.get("concept_id")
            for item in [
                *actor_packet.get("trusted_knowledge", []),
                *actor_packet.get("supported_knowledge", []),
            ]
            if item.get("entity_id") or item.get("concept_id")
        ]
        player_proposal = {
            "agent_id": player_profile.get("agent_id", player_id),
            "character_id": player_id,
            "canonical_name": player_profile.get("canonical_name"),
            "dialogue": "",
            "action_intent": {
                "action_type": clean_text(player_intent.get("action_type")) or "minor_action",
                "description": player_intent["resolved_intent"],
                "impact_level": clean_text(player_intent.get("impact_level")) or "minor_action",
                "target_concept_ids": [
                    clean_text(item)
                    for item in player_intent.get("target_concept_ids", [])
                    if clean_text(item)
                ],
                "ability_concept_id": "",
                "artifact_concept_id": "",
                "candidate_rule_ids": [],
                "proposed_state_changes": [],
            },
            "concept_refs": [],
            "claims": [],
            "private_reasoning_summary": player_intent.get("thought_assimilation", ""),
        }
        validations.append(
            self.validator.validate(
                player_proposal,
                player_id,
                self.store,
                player_rag_ids,
            )
        )
        for proposal in npc_actions:
            actor_packet = (
                context.get("rag_orchestration", {})
                .get("agent_packets", {})
                .get(proposal["character_id"], {})
            )
            actor_rag_ids = [
                item.get("entity_id") or item.get("concept_id")
                for item in [
                    *actor_packet.get("trusted_knowledge", []),
                    *actor_packet.get("supported_knowledge", []),
                ]
                if item.get("entity_id") or item.get("concept_id")
            ]
            validation = self.validator.validate(
                proposal,
                proposal["character_id"],
                self.store,
                actor_rag_ids,
            )
            validations.append(validation)
        stage_seconds["rules_agent_validation"] = round(
            time.perf_counter() - stage_started,
            3,
        )

        should_run_local_world = bool(
            wake_plan.get("should_run_local_world")
            or npc_requires_world_or_gm
            or self._group_controller_ran(group_controller)
        )
        if should_run_local_world:
            self._progress(progress_callback, 58, "局部世界 Agent 正在更新现场")
            stage_started = time.perf_counter()
            try:
                local_world = self._local_world_agent(
                    player_intent,
                    npc_actions,
                    group_controller,
                    elapsed_minutes,
                    context,
                )
            except Exception:
                local_world = {
                    "world_changes": [],
                    "npc_position_updates": [],
                    "object_updates": [],
                    "new_events": [],
                    "scene_transition": {},
                    "ambient_npc_reactions": [],
                    "sensory_environment": {},
                    "encyclopedia_updates": [],
                }
            stage_seconds["local_world_agent_llm"] = round(
                time.perf_counter() - stage_started,
                3,
            )
        else:
            local_world = {
                "world_changes": [],
                "npc_position_updates": [],
                "object_updates": [],
                "new_events": [],
                "scene_transition": {},
                "ambient_npc_reactions": [],
                "sensory_environment": {},
                "encyclopedia_updates": [],
            }
            stage_seconds["local_world_agent_llm"] = 0.0
        for key, default in (
            ("world_changes", []),
            ("npc_position_updates", []),
            ("object_updates", []),
            ("new_events", []),
            ("scene_transition", {}),
            ("ambient_npc_reactions", []),
            ("sensory_environment", {}),
            ("encyclopedia_updates", []),
        ):
            local_world.setdefault(key, default)
        if not isinstance(local_world.get("scene_transition"), dict):
            local_world["scene_transition"] = {}
        if not isinstance(local_world.get("ambient_npc_reactions"), list):
            local_world["ambient_npc_reactions"] = []
        local_world = self._merge_group_controller_into_local_world(
            local_world,
            group_controller,
        )
        local_world = self._apply_local_world_autonomy(
            local_world,
            player_intent,
            elapsed_minutes,
            context,
        )

        should_run_gm = bool(
            wake_plan.get("should_run_gm")
            or npc_requires_world_or_gm
            or self._group_controller_ran(group_controller)
        )
        if should_run_gm:
            self._progress(progress_callback, 66, "世界主持人正在裁定本轮结果")
            stage_started = time.perf_counter()
            try:
                resolution = self._gm_resolver(
                    player_intent,
                    npc_actions,
                    validations,
                    local_world,
                    context,
                )
            except Exception:
                resolution = {
                    "success": True,
                    "outcome": "deferred",
                    "consequences": [],
                    "state_changes": [],
                    "resolved_actions": [],
                    "player_action_addressed": True,
                    "impact_level": player_intent.get("impact_level", "minor_action"),
                    "diverges_from_canon": False,
                    "divergence_reason": "",
                    "canonical_event_status": "unchanged",
                }
            stage_seconds["gm_resolver_llm"] = round(
                time.perf_counter() - stage_started,
                3,
            )
        else:
            resolution = {
                "success": True,
                "outcome": "success",
                "consequences": [],
                "state_changes": [],
                "resolved_actions": [
                    {
                        "actor_id": player_id,
                        "description": player_intent["resolved_intent"],
                        "outcome": "success",
                        "state_changes": [],
                    }
                ],
                "player_action_addressed": True,
                "impact_level": player_intent.get("impact_level", "minor_action"),
                "diverges_from_canon": False,
                "divergence_reason": "",
                "canonical_event_status": "unchanged",
            }
            stage_seconds["gm_resolver_llm"] = 0.0
        if not isinstance(resolution, dict):
            resolution = {}
        resolution.setdefault("outcome", "deferred")
        resolution.setdefault("consequences", [])
        resolution.setdefault("state_changes", [])
        resolution.setdefault("resolved_actions", [])
        resolution.setdefault("player_action_addressed", True)
        resolution.setdefault("impact_level", player_intent.get("impact_level", "minor_action"))
        resolution.setdefault("diverges_from_canon", False)
        resolution.setdefault("divergence_reason", "")
        resolution.setdefault("canonical_event_status", "unchanged")
        resolution["state_changes"] = self._normalize_state_changes_for_commit(
            resolution.get("state_changes", [])
        )
        if (
            not resolution.get("diverges_from_canon")
            and self._intent_implies_canon_divergence(player_intent)
            and clean_text(resolution.get("outcome")).lower()
            not in {"failed", "blocked"}
        ):
            resolution["diverges_from_canon"] = True
            resolution["canonical_event_status"] = "altered"
            resolution["divergence_reason"] = (
                clean_text(resolution.get("divergence_reason"))
                or "玩家注入念头改变了角色对当前原著压力的判断。"
            )
        if (
            bool(resolution.get("diverges_from_canon"))
            and clean_text(resolution.get("canonical_event_status")).lower()
            == "advanced"
        ):
            resolution["canonical_event_status"] = "altered"

        self._progress(progress_callback, 72, "事件调度 Agent 正在整理原著压力")
        stage_started = time.perf_counter()
        event_scheduler = self._narrative_spine_update(resolution)
        stage_seconds["event_scheduler"] = round(
            time.perf_counter() - stage_started,
            3,
        )

        resolved_actions = [
            item
            for item in resolution.get("resolved_actions", [])
            if isinstance(item, dict)
        ]
        if not any(
            clean_text(item.get("actor_id")) == player_id
            for item in resolved_actions
        ):
            resolved_actions.insert(
                0,
                {
                    "actor_id": player_id,
                    "description": player_intent["resolved_intent"],
                    "outcome": clean_text(
                        resolution.get("outcome")
                    ) or "deferred",
                    "state_changes": [],
                },
            )
        resolution["resolved_actions"] = resolved_actions
        resolution["player_action_addressed"] = True

        self._progress(progress_callback, 78, "叙事 Agent 正在写成本轮正文")
        stage_started = time.perf_counter()
        narrative = self._scene_renderer(
            player_profile,
            user_input,
            player_intent,
            npc_actions,
            local_world,
            resolution,
            elapsed_minutes,
            context=context,
        )
        narrative = self._dedupe_repeated_paragraphs(narrative)
        narrative = self._dedupe_adjacent_paragraphs(narrative)
        if self._looks_like_structured_output(narrative):
            narrative = self._fallback_narrative(
                user_input,
                player_intent,
                npc_actions,
                local_world,
                resolution,
            )
        stage_seconds["scene_renderer_llm"] = round(
            time.perf_counter() - stage_started,
            3,
        )
        state_changes = [
            item
            for item in resolution.get("state_changes", [])
            if isinstance(item, dict)
        ]
        event_id = "event_" + uuid.uuid4().hex[:16]
        runtime_updates = self._runtime_updates(
            player_id,
            player_intent,
            npc_actions,
            local_world,
            resolution,
            raw_user_input=user_input,
            context=context,
            event_id=event_id,
        )
        committed_participants = compact_list(
            (
                runtime_updates.get("active_scene", {}).get("participant_ids")
                or [player_id, *nearby_ids]
            ),
            self.max_nearby_agents + 1,
        )
        event = {
            "event_id": event_id,
            "idempotency_key": stable_hash(
                {
                    "branch": self.store.branch["branch_id"],
                    "revision": self.store.branch["head_revision"],
                    "user_input": clean_text(user_input),
                    "player_id": player_id,
                }
            ),
            "event_type": "immersive_scene_turn",
            "impact_level": clean_text(
                resolution.get("impact_level")
            ) or "minor_action",
            "status": "completed",
            "participants": committed_participants,
            "visible_to": committed_participants,
            "narration": narrative,
            "player_id": player_id,
            "player_input": clean_text(user_input),
            "dialogue": [
                {
                    "speaker_id": item["character_id"],
                    "speaker_name": item["canonical_name"],
                    "text": item.get("dialogue", ""),
                }
                for item in npc_actions
                if item.get("dialogue")
            ]
            + [
                {
                    "speaker_id": f"ambient_npc_{index}",
                    "speaker_name": clean_text(
                        reaction.get("speaker_label")
                    )
                    or "附近路人",
                    "text": clean_text(reaction.get("dialogue")),
                }
                for index, reaction in enumerate(
                    local_world.get("ambient_npc_reactions", []),
                    start=1,
                )
                if isinstance(reaction, dict)
                and clean_text(reaction.get("dialogue"))
            ],
            "player_intent": player_intent,
            "npc_agent_outputs": npc_actions,
            "local_world": local_world,
            "gm_resolution": resolution,
            "story_spine_before": context.get("story_spine", {}),
            "rag_query_plan": context.get("query_plan", {}),
            "rag_orchestration_summary": {
                "actor_packet_ids": context.get("rag_orchestration", {}).get(
                    "actor_packet_ids", []
                ),
                "policy": context.get("rag_orchestration", {}).get(
                    "policy", {}
                ),
            },
            "action_intents": [
                {
                    "actor_id": player_id,
                    "action_type": clean_text(
                        player_intent.get("action_type")
                    ) or "player_intent",
                    "description": player_intent["resolved_intent"],
                    "impact_level": clean_text(
                        player_intent.get("impact_level")
                    ) or "minor_action",
                },
                *[
                    {
                        "actor_id": item["character_id"],
                        **item["action_intent"],
                    }
                    for item in npc_actions
                ],
            ],
            "resolved_actions": resolution.get(
                "resolved_actions", []
            ),
            "state_changes": state_changes,
            "runtime_updates": runtime_updates,
            "elapsed_minutes": elapsed_minutes,
            "duration_reason": clean_text(time_result.get("reason")),
            "clock_transition": self.store.clock_after_minutes(
                elapsed_minutes
            ),
            "backend_stage": "multi_agent_immersive_pipeline",
            "created_at": utc_now(),
        }
        global_trigger = bool(
            time_result.get("triggers_global_update")
            or elapsed_minutes >= 120
            or clean_text(player_intent.get("action_type")).lower()
            in {"travel", "sleep", "fast_forward", "leave_region"}
            or event["impact_level"] == "high_impact"
        )
        if global_trigger:
            self._progress(progress_callback, 88, "大世界 Agent 正在响应重大变化")
            stage_started = time.perf_counter()
            projection = self._world_project(event, context)
            stage_seconds["global_world_llm"] = round(
                time.perf_counter() - stage_started,
                3,
            )
            event["world_projection"] = projection
            event["backend_stage"] = "global_world_projection"
            event["state_changes"].extend(
                item
                for item in projection.get("state_changes", [])
                if isinstance(item, dict)
            )
        final_validation = self._event_validation(event, validations)
        stage_started = time.perf_counter()
        commit = self.store.commit_event(event, final_validation)
        self._progress(progress_callback, 94, "记忆记录 Agent 正在写入经历")
        memory_summary_ran = bool(
            self.memory_summary_interval
            and self.store.branch["head_revision"]
            % self.memory_summary_interval
            == 0
        )
        self._summarize_memories([player_profile, *profiles])
        stage_seconds["commit_and_memory"] = round(
            time.perf_counter() - stage_started,
            3,
        )
        stage_seconds["total"] = round(time.perf_counter() - turn_started, 3)
        self._progress(progress_callback, 100, "本轮完成")
        return {
            "event": event,
            "commit": commit,
            "state_revision": self.store.branch["head_revision"],
            "branch_id": self.store.branch["branch_id"],
            "pipeline": {
                "multi_agent_pipeline": True,
                "planner_mode": "disabled_default",
                "effective_scene_before_turn": turn_scene,
                "active_scene_after_turn": runtime_updates.get(
                    "active_scene", {}
                ),
                "pre_turn_location_inference": pre_location,
                "promoted_ambient_npc_ids": promoted_ambient_ids,
                "player_controller": player_intent,
                "time_agent": time_result,
                "agent_scheduler": wake_plan,
                "nearby_npc_agents": npc_actions,
                "sleeping_npc_agents": skipped_npc_actions,
                "group_controller": group_controller,
                "group_controller_ran": self._group_controller_ran(
                    group_controller
                ),
                "npc_requires_world_or_gm": npc_requires_world_or_gm,
                "local_world_agent": local_world,
                "local_world_agent_ran": should_run_local_world,
                "rules_agent": {
                    "validation_count": len(validations),
                    "statuses": [
                        item.get("status") for item in validations
                    ],
                },
                "gm_resolver": resolution,
                "gm_resolver_ran": should_run_gm,
                "event_scheduler": event_scheduler,
                "memory_agent": {
                    "event_memory_recorded": True,
                    "summary_compaction_ran": memory_summary_ran,
                    "visible_actor_count": len([player_profile, *profiles]),
                },
                "scene_renderer": {
                    "character_count": len(narrative),
                    "strict_first_person": True,
                    "single_pass": False,
                    "json_free_text": True,
                },
                "global_world_agent_ran": global_trigger,
                "story_spine_after": self.store.runtime.get(
                    "narrative_spine", {}
                ),
                "rag_query_plan": context.get("query_plan", {}),
                "stage_seconds": stage_seconds,
            },
            "internal_validation": {
                "proposal_validations": validations,
                "event_validation": final_validation,
            },
        }


def load_step17_runtime(
    world_path=Path("world_db.json"),
    character_path=Path("character_state_db.json"),
    agent_path=Path("agent_profiles.json"),
    state_path=Path("simulation_state.json"),
    llm_callable=None,
):
    world_path = Path(world_path)
    world_db = json.loads(world_path.read_text(encoding="utf-8"))
    world_db = load_layer_sidecars(world_db, world_path.parent)
    generated_root = world_path.parent.parent
    runtime_dir = generated_root / "runtime"
    if runtime_dir.is_dir():
        world_db = load_layer_sidecars(world_db, runtime_dir)
    for filename, key in (
        ("canonical_relationship_db.json", "canonical_relationship_db"),
        ("canonical_relationships_db.json", "canonical_relationships_db"),
        ("canonical_scene_beat_db.json", "canonical_scene_beat_db"),
        ("relationship_arc_db.json", "relationship_arc_db"),
        ("runtime_event_db.json", "runtime_event_db"),
        ("runtime_relationship_db.json", "runtime_relationship_db"),
        ("runtime_log.json", "runtime_log"),
    ):
        for sidecar_dir in (world_path.parent, runtime_dir):
            sidecar = sidecar_dir / filename
            if key not in world_db and sidecar.is_file():
                world_db[key] = json.loads(sidecar.read_text(encoding="utf-8"))
                break
    if "relationship_system" not in world_db and (
        "canonical_relationship_db" in world_db
        or "canonical_relationships_db" in world_db
        or "relationship_arc_db" in world_db
    ):
        world_db["relationship_system"] = {
            "canonical_relationship_db": world_db.get(
                "canonical_relationship_db", {}
            ),
            "canonical_relationships_db": world_db.get(
                "canonical_relationships_db", {}
            ),
            "relationship_arc_db": world_db.get("relationship_arc_db", {}),
        }
    character_db = json.loads(Path(character_path).read_text(encoding="utf-8"))
    agent_profiles = json.loads(Path(agent_path).read_text(encoding="utf-8"))
    store = SimulationStore(
        world_db,
        character_db,
        agent_profiles,
        path=state_path,
    )
    if llm_callable is None:
        return {
            "world_db": world_db,
            "character_db": character_db,
            "agent_profiles": agent_profiles,
            "store": store,
            "validator": WorldValidator(
                world_db, character_db, agent_profiles
            ),
        }
    orchestrator = ImmersiveSimulationOrchestrator(
        world_db,
        character_db,
        agent_profiles,
        store,
        llm_callable,
    )
    return {
        "world_db": world_db,
        "character_db": character_db,
        "agent_profiles": agent_profiles,
        "store": store,
        "validator": orchestrator.validator,
        "orchestrator": orchestrator,
    }
