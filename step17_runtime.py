import copy
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from world_state_layers import (
    build_runtime_event_db,
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
        self.state = self._load_or_create()

    def _base_entity_states(self):
        layered_state = (
            self.world_db.get("simulation_state_db", {})
            .get("current_world_state", {})
        )
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
        layered_state = (
            self.world_db.get("simulation_state_db", {})
            .get("current_world_state", {})
        )
        return deep_copy(layered_state.get("resource_states", {}))

    def _base_relationship_states(self):
        layered_state = (
            self.world_db.get("simulation_state_db", {})
            .get("current_world_state", {})
        )
        states = deep_copy(layered_state.get("relationship_states", {}))
        cutoff_order = self.world_db.get("simulation_state_db", {}).get(
            "cutoff_order"
        )
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
        layered_state = (
            self.world_db.get("simulation_state_db", {})
            .get("current_world_state", {})
        )
        return deep_copy(layered_state.get("identity_states", {}))

    def _base_runtime_events(self):
        return deep_copy(self.world_db.get("runtime_event_db", {}))

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
            "location_runtime": {},
            "active_events": [],
            "runtime_event_db": self._base_runtime_events(),
            "runtime_event_queue": self._base_runtime_events().get(
                "event_queue", []
            ),
            "canonical_timeline": [],
            "timeline_cursor": 0,
            "branch_records": [],
            "long_term_memories": {},
            "world_knowledge_cache": {},
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
                    "location_runtime",
                    "active_events",
                    "resource_states",
                    "identity_states",
                    "runtime_event_db",
                    "runtime_event_queue",
                    "canonical_timeline",
                    "timeline_cursor",
                    "branch_records",
                    "long_term_memories",
                    "world_knowledge_cache",
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

    def save(self):
        self.state["updated_at"] = utc_now()
        atomic_write_json(self.path, self.state)

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
        event = {
            "event_id": "event_" + uuid.uuid4().hex[:16],
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
        participants = list(dict.fromkeys([focus_character_id, *participant_ids]))
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
        return profile

    def _dynamic_profile(self, character_id):
        if character_id in self.agent_by_character_id:
            return self._profile_with_current_capabilities(
                self.agent_by_character_id[character_id]
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
            "state": {
                "background_summary": character.get("background_summary", ""),
                "personality": character.get("personality", []),
                "goals": character.get("goals", []),
                "constraints": character.get("constraints", []),
                "speech_styles": character.get("speech_styles", []),
                "knowledge_scope": character.get("knowledge_scope", []),
            },
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
        return self._profile_with_current_capabilities(profile)

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
        return rows

    def _search_terms(self, text, profiles):
        terms = set(re.findall(r"[\w\u4e00-\u9fff]{2,}", clean_text(text)))
        for profile in profiles:
            terms.add(profile["canonical_name"])
            terms.update(profile.get("identity", {}).get("aliases", []))
        return {item.casefold() for item in terms if item}

    def build_context_packet(self, user_input, profiles):
        scene = self.store.runtime.get("active_scene") or {}
        terms = self._search_terms(user_input, profiles)
        scored = []
        for unit in self.world_db.get("knowledge_units", []):
            status = unit.get("model_status", "unresolved")
            if status not in {"trusted", "supported"}:
                continue
            haystack = " ".join(
                [
                    unit.get("name", ""),
                    *unit.get("retrieval_tags", []),
                    *unit.get("descriptions", []),
                ]
            ).casefold()
            score = sum(term in haystack for term in terms)
            if score:
                scored.append((score, status == "trusted", unit))
        scored.sort(key=lambda row: (-row[0], not row[1], row[2]["name"]))
        units = [deep_copy(row[2]) for row in scored[: self.max_context_units]]
        return {
            "scene": scene,
            "state_revision": self.store.branch["head_revision"],
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
        }

    def _agent_prompt(self, profile, user_input, context):
        memory = self.store.runtime.get("agent_memories", {}).get(
            profile["character_id"], {}
        )
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
                "state": profile.get("state", {}),
                "capabilities": profile.get("capabilities", {}),
                "relationships": profile.get("relationships", [])[:12],
                "guardrails": profile.get("guardrails", {}),
            },
            "runtime_memory": memory,
            "context": context,
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
        system = """
你是世界推演 Agent，负责当前场景之外的连锁反应。只依据已裁定事件、
只读世界事实和当前 simulation state 推演。不要替当前场景角色说台词。
没有足够证据时不产生变化。只输出 JSON。
""".strip()
        user = f"""
已裁定事件：{json.dumps(event, ensure_ascii=False)}
相关世界上下文：{json.dumps(context, ensure_ascii=False)}
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

    def _summarize_memories(self, profiles):
        if self.store.branch["head_revision"] % self.memory_summary_interval:
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
            system = """
把角色可见的近期事件压缩成短期记忆摘要。只写该角色知道的内容，
保留关键人物、物品、地点、承诺和未解决冲突。不得补充外部常识。
只输出 JSON：{"summary":"..."}。
""".strip()
            try:
                payload = self._call_json(
                    system,
                    json.dumps(
                        {
                            "character": profile["canonical_name"],
                            "previous_summary": memory.get("summary", ""),
                            "visible_events": events,
                        },
                        ensure_ascii=False,
                    ),
                    max_tokens=700,
                )
                self.store.update_memory_summary(
                    character_id, payload.get("summary", "")
                )
            except Exception:
                continue

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

    def _last_visible_narrative(self):
        for event in reversed(self.store.branch["events"]):
            if (
                event.get("narration")
                and event.get("event_type")
                in {"scene_opening_rendered", "immersive_scene_turn"}
            ):
                return event["narration"]
        return ""

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
        for value in record.get("source_chunk_ids", []):
            try:
                values.append(int(value))
            except (TypeError, ValueError):
                pass
        for item in record.get("evidence", []):
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
        return timeline

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
        max_tokens=6200,
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
        system = """
你是 Player Character Controller。玩家控制原著角色的行为，但不能抹除
角色身份、记忆、关系、长期目标与已知能力。把玩家输入解释成该角色此刻
真正会尝试的意图；若明显违背人格，只标记冲突并将其转译为带有迟疑、
挣扎或需要更强动机的尝试，不替玩家拒绝一切偏离。只输出 JSON。
""".strip()
        user = json.dumps(
            {
                "character": profile,
                "runtime_state": self.store.runtime.get(
                    "character_runtime", {}
                ).get(profile["character_id"], {}),
                "visible_context": context,
                "player_input": user_input,
                "required_output_language": self._response_language(
                    user_input
                ),
                "output_schema": {
                    "character": profile["canonical_name"],
                    "player_input": user_input,
                    "character_context": "",
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

    def _perception_packet(self, profile, shared_context):
        scene = self.store.runtime.get("active_scene") or {}
        character_id = profile["character_id"]
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
            "memory": self.store.runtime.get("agent_memories", {}).get(
                character_id, {}
            ),
            "known_information": runtime_state.get(
                "known_information", []
            ),
            "retrieved_knowledge": shared_context,
            "epistemic_rule": (
                "只能使用亲眼看见、亲耳听见、亲身经历、被明确告知或角色"
                "记忆中已有的信息；其他角色内心与场外事件均不可知。"
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
player_resolved_intent 是玩家角色将要执行或刚执行的动作。你只能对此作出
自己的反应，绝对不能替玩家说出同一句话、抢先执行相同动作，或把玩家的
目标据为自己的目标。生理信息只能填写当前证据明确支持的事实，不得把武魂、
能力或外号推断成种族。只输出 JSON。
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
        elapsed_minutes,
        context,
    ):
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
你还负责为新出现且缓存中没有说明的物品与能力写面向未读过原著用户的
简明解释；已有缓存的概念不会发给你，禁止重复改写。只输出 JSON。
""".strip()
        return self._call_json(
            system,
            json.dumps(
                {
                    "scene": self.store.runtime.get("active_scene"),
                    "location_runtime": self.store.runtime.get(
                        "location_runtime", {}
                    ),
                    "player_intent": player_intent,
                    "npc_actions": npc_actions,
                    "elapsed_minutes": elapsed_minutes,
                    "context": context,
                    "uncached_concepts_requiring_explanation": (
                        missing_concepts
                    ),
                    "output_schema": {
                        "world_changes": [],
                        "npc_position_updates": [],
                        "object_updates": [],
                        "new_events": [],
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

    def _gm_resolver(
        self,
        player_intent,
        npc_actions,
        validations,
        local_world,
        context,
    ):
        system = """
你是 GM Resolver，只做规则裁定，不写场景、不写文学叙述、不代替角色发言。
裁定玩家和 NPC 的尝试是否成功以及可提交的状态变化。原著事件是默认会继续
存在的历史压力，不是强制脚本；玩家可以改变结果。
必须把 player_intent 作为第一项 resolved_actions 明确裁定。不能忽略、
替换或偷偷改成上一轮的行动；若人格冲突，也要让角色实际说出或做出玩家
要求的尝试，再通过迟疑、语气、生理反应和后果表现冲突。只输出 JSON。
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
                    "context": context,
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

    def current_canonical_event(self):
        cursor = int(self.store.runtime.get("timeline_cursor", 0))
        timeline = (
            self.store.runtime.get("canonical_timeline")
            or self.canonical_timeline
        )
        if not timeline:
            return {}
        return deep_copy(timeline[min(cursor, len(timeline) - 1)])

    def _renderer_prompt(
        self,
        player_profile,
        raw_player_input,
        player_intent,
        npc_actions,
        local_world,
        resolution,
        elapsed_minutes,
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
6. 开场轮必须处于角色原生的正常生活轨迹，不用突发灾难强行开戏。
7. 正文不少于 1500 个中文字符，分成自然段，停在一个可继续行动的时刻。
8. 非开场轮必须从上一轮最后时刻继续。第一段就落实本轮玩家输入，不得从
清晨、起床、场景介绍或前一轮开头重新写起，不得复述上一轮已经发生的过程。
9. 玩家输入是本轮硬约束。即使它违背角色人格，角色也必须实际尝试说出或
做出该行为；人格冲突只能改变表现方式和后果，不能把指令换成别的行动。
10. 全文语言必须与玩家本轮输入语言一致。
""".strip()
        previous = self._last_visible_narrative()
        user = json.dumps(
            {
                "mode": "canonical_daily_opening" if opening else "turn_result",
                "raw_player_input_must_appear_as_action": raw_player_input,
                "required_output_language": self._response_language(
                    raw_player_input
                ),
                "viewpoint_character": player_profile,
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
                "elapsed_minutes": elapsed_minutes,
                "active_scene": self.store.runtime.get("active_scene"),
                "canonical_event": self.current_canonical_event(),
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

    def _scene_renderer(self, *args, **kwargs):
        system, user = self._renderer_prompt(*args, **kwargs)
        narrative = self._call_text(system, user)
        opening = bool(kwargs.get("opening", False))
        previous = self._last_visible_narrative()
        if not opening and previous:
            common_prefix = 0
            for left, right in zip(previous, narrative):
                if left != right:
                    break
                common_prefix += 1
            if common_prefix >= 120:
                narrative = self._call_text(
                    system,
                    json.dumps(
                        {
                            "instruction": (
                                "此稿错误地重写了上一轮。完全丢弃此稿。"
                                "从 previous_ending_only 之后开始，第一段立即"
                                "执行 raw_player_input_must_appear_as_action。"
                                "禁止复用上一轮开头或重新介绍场景。"
                            ),
                            "original_payload": user,
                        },
                        ensure_ascii=False,
                    ),
                    temperature=0.72,
                    max_tokens=6200,
                )
        if not opening:
            raw_player_input = (
                args[1]
                if len(args) > 1
                else kwargs.get("raw_player_input", "")
            )
            guard = self._call_json(
                """
你是小说回合验收器。判断候选正文是否满足：
1. 前三段内由第一人称“我”实际执行或说出本轮玩家输入，而不是其他 NPC
抢先代做；2. 没有从上一轮开头重写；3. 没有把玩家指令换成别的行动；
4. 语言与玩家输入一致。只输出 JSON。
""".strip(),
                json.dumps(
                    {
                        "player_input": raw_player_input,
                        "previous_ending": previous[-700:],
                        "candidate_opening": narrative[:1600],
                        "output_schema": {
                            "passed": True,
                            "player_action_visible_early": True,
                            "npc_stole_player_action": False,
                            "restarts_previous_scene": False,
                            "language_matches": True,
                            "reason": "",
                        },
                    },
                    ensure_ascii=False,
                ),
                max_tokens=500,
            )
            if not guard.get("passed"):
                narrative = self._call_text(
                    system,
                    json.dumps(
                        {
                            "instruction": (
                                "候选正文未通过回合验收，必须完整重写。"
                                "第一段以“我”的即时动作开始，在前三段内"
                                "明确执行玩家原始输入。NPC 只能在玩家行动后"
                                "反应，不能抢先说出或做出玩家的动作。不要"
                                "复述旧剧情，语言与玩家输入一致。"
                            ),
                            "guard_failure": guard,
                            "original_payload": user,
                        },
                        ensure_ascii=False,
                    ),
                    temperature=0.72,
                    max_tokens=6200,
                )
        attempts = 0
        while (
            self.min_narrative_chars
            and len(narrative) < self.min_narrative_chars
            and attempts < 4
        ):
            continuation = self._call_text(
                system,
                json.dumps(
                    {
                        "instruction": (
                            "从上一稿最后一句之后直接续写正文，不要重写、"
                            "不要总结、不要解释、不要添加标题。继续保持同一"
                            "第一人称感知边界和渐进节奏，直到故事自然推进到"
                            f"总长度至少 {self.min_narrative_chars} 个中文字符。"
                        ),
                        "current_character_count": len(narrative),
                        "draft_tail": narrative[-1800:],
                        "original_payload": user,
                    },
                    ensure_ascii=False,
                ),
                temperature=0.78,
                max_tokens=4200,
            )
            if continuation:
                narrative = narrative.rstrip() + "\n\n" + continuation.lstrip()
            attempts += 1
        if (
            self.min_narrative_chars
            and len(narrative) < self.min_narrative_chars
        ):
            raise ValueError(
                "Scene Renderer output is shorter than the required "
                f"{self.min_narrative_chars} characters."
            )
        return narrative

    def _runtime_updates(
        self,
        player_id,
        player_intent,
        npc_actions,
        local_world,
        resolution,
    ):
        def meaningful_state_update(character_id, value):
            if not isinstance(value, dict):
                return {}
            result = {}
            for key, item in value.items():
                if item in (None, "", [], {}):
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
                item.get("self_state_update")
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
            player_intent.get("self_state_update")
        )
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
        scene = self.store.runtime.get("active_scene") or {}
        location_id = scene.get("location_id")
        location_updates = {}
        if location_id:
            sensory = local_world.get("sensory_environment", {})
            location_updates[location_id] = {
                "location_id": location_id,
                **deep_copy(RUNTIME_LOCATION_DEFAULTS),
                **sensory,
                "present_characters": scene.get("participant_ids", []),
                "ongoing_events": local_world.get("new_events", []),
            }
        return {
            "character_runtime": character_updates,
            "location_runtime": location_updates,
            "active_events": local_world.get("new_events", []),
            "branch_records": branch_records[-100:],
            "world_knowledge_cache": self._world_cache_updates(
                [player_id, *[item["character_id"] for item in npc_actions]],
                local_world.get("encyclopedia_updates", []),
            ),
        }

    def start_character_experience(
        self, character_id, progress_callback=None
    ):
        self._progress(progress_callback, 5, "定位角色的原著出场阶段")
        timeline_index, anchor = self._opening_anchor(character_id)
        order = anchor.get(
            "scheduled_order", self._character_entry_order(character_id)
        )
        cutoff_state_db, cutoff_runtime_db = self._cutoff_databases(order)
        cutoff_world_state = cutoff_state_db.get("current_world_state", {})
        cutoff_resource_states = cutoff_world_state.get("resource_states", {})
        nearby = compact_list(
            [
                *anchor.get("participants", []),
                *self._opening_cast(character_id, order),
            ],
            self.max_nearby_agents,
        )
        nearby = [
            item
            for item in nearby
            if item != character_id and item in self.character_by_id
        ]
        location_id = (
            anchor.get("location_id") or self._nearest_location(order)
        )
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
        local_world = {
            "world_changes": [],
            "npc_position_updates": [],
            "object_updates": [],
            "new_events": [anchor.get("event", "原著日常")],
            "sensory_environment": {},
        }
        self._progress(progress_callback, 58, "角色正在进入日常生活")
        opening = self._scene_renderer(
            profile,
            "继续此刻原本正在进行的日常生活",
            {
                "resolved_intent": "继续此刻原本正在进行的日常生活",
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
        scene = self.store.runtime.get("active_scene")
        if not scene:
            raise RuntimeError("Start a character experience first.")
        player_id = clean_text(scene.get("focus_character_id"))
        player_profile = self._dynamic_profile(player_id)
        nearby_ids = [
            item
            for item in scene.get("participant_ids", [])
            if item != player_id
        ][: self.max_nearby_agents]
        profiles = [self._dynamic_profile(item) for item in nearby_ids]
        context = self.build_context_packet(
            user_input, [player_profile, *profiles]
        )
        self._progress(progress_callback, 8, "角色正在理解你的行动")
        player_intent = self._player_controller(
            player_profile, user_input, context
        )
        self._progress(progress_callback, 18, "时间 Agent 正在估算经过时间")
        time_result = self._time_agent(player_intent, user_input)
        elapsed_minutes = bounded_int(
            time_result.get("elapsed_minutes"),
            default=0,
            minimum=0,
        )
        npc_actions = []
        validations = []
        rag_ids = [
            item["entity_id"]
            for item in [
                *context["trusted_knowledge"],
                *context["supported_knowledge"],
            ]
        ]
        for profile in profiles:
            completed = len(npc_actions)
            self._progress(
                progress_callback,
                25 + round(
                    28 * completed / max(1, len(profiles))
                ),
                f"{profile['canonical_name']} 正在观察并行动",
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
                        "visible_behavior": "继续原本的活动",
                        "action_intent": {
                            "action_type": "continue_activity",
                            "description": "继续原本的活动并留意周围",
                            "impact_level": "minor_action",
                        },
                        "private_reasoning_summary": clean_text(error),
                    },
                ) | {
                    "perception": "",
                    "emotion": "",
                    "goal": "",
                    "visible_behavior": "继续原本的活动",
                }
            validation = self.validator.validate(
                proposal,
                profile["character_id"],
                self.store,
                rag_ids,
            )
            npc_actions.append(proposal)
            validations.append(validation)
        self._progress(progress_callback, 55, "局部世界正在推进环境与事件")
        local_world = self._local_world_agent(
            player_intent,
            npc_actions,
            elapsed_minutes,
            context,
        )
        self._progress(progress_callback, 68, "GM 正在裁定行动结果")
        resolution = self._gm_resolver(
            player_intent,
            npc_actions,
            validations,
            local_world,
            context,
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
        self._progress(progress_callback, 78, "场景 Renderer 正在写作本轮小说")
        narrative = self._scene_renderer(
            player_profile,
            user_input,
            player_intent,
            npc_actions,
            local_world,
            resolution,
            elapsed_minutes,
        )
        state_changes = [
            item
            for item in resolution.get("state_changes", [])
            if isinstance(item, dict)
        ]
        event = {
            "event_id": "event_" + uuid.uuid4().hex[:16],
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
            "participants": [player_id, *nearby_ids],
            "visible_to": [player_id, *nearby_ids],
            "narration": narrative,
            "dialogue": [
                {
                    "speaker_id": item["character_id"],
                    "speaker_name": item["canonical_name"],
                    "text": item.get("dialogue", ""),
                }
                for item in npc_actions
                if item.get("dialogue")
            ],
            "player_intent": player_intent,
            "npc_agent_outputs": npc_actions,
            "local_world": local_world,
            "gm_resolution": resolution,
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
            "runtime_updates": self._runtime_updates(
                player_id,
                player_intent,
                npc_actions,
                local_world,
                resolution,
            ),
            "elapsed_minutes": elapsed_minutes,
            "duration_reason": clean_text(time_result.get("reason")),
            "clock_transition": self.store.clock_after_minutes(
                elapsed_minutes
            ),
            "backend_stage": "immersive_local_pipeline",
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
            self._progress(progress_callback, 90, "大世界正在响应重大变化")
            projection = self._world_project(event, context)
            event["world_projection"] = projection
            event["backend_stage"] = "global_world_projection"
            event["state_changes"].extend(
                item
                for item in projection.get("state_changes", [])
                if isinstance(item, dict)
            )
        final_validation = self._event_validation(event, validations)
        commit = self.store.commit_event(event, final_validation)
        self._progress(progress_callback, 96, "保存角色状态与世界进度")
        self._summarize_memories([player_profile, *profiles])
        self._progress(progress_callback, 100, "本轮完成")
        return {
            "event": event,
            "commit": commit,
            "state_revision": self.store.branch["head_revision"],
            "branch_id": self.store.branch["branch_id"],
            "pipeline": {
                "player_controller": player_intent,
                "time_agent": time_result,
                "nearby_npc_agents": npc_actions,
                "local_world_agent": local_world,
                "gm_resolver": resolution,
                "scene_renderer": {
                    "character_count": len(narrative),
                    "strict_first_person": True,
                },
                "global_world_agent_ran": global_trigger,
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
    for filename, key in (
        ("canonical_relationships_db.json", "canonical_relationships_db"),
        ("relationship_arc_db.json", "relationship_arc_db"),
    ):
        sidecar = world_path.parent / filename
        if key not in world_db and sidecar.is_file():
            world_db[key] = json.loads(sidecar.read_text(encoding="utf-8"))
    if "relationship_system" not in world_db and (
        "canonical_relationships_db" in world_db or "relationship_arc_db" in world_db
    ):
        world_db["relationship_system"] = {
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
