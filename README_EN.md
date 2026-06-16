# NavelMaker 2 Desktop Guide

NavelMaker 2 Desktop is a local novel-preparation and simulation tool. It converts a novel TXT file into layered JSON databases that support long-running character simulation, world evolution, Agent decisions, user intervention, and branches that can diverge from the original plot.

## Quick Start

Run these batch files in order:

```bat
01_install_requirements.bat
02_prepare_simulation.bat
03_run_simulation.bat
```

`01_install_requirements.bat` installs the Python dependencies.

`02_prepare_simulation.bat` opens the preparation UI. Select a novel TXT file, choose the story percentage, verify the local LLM settings, and start the preparation pipeline.

`03_run_simulation.bat` checks the required databases and launches the standalone simulation UI.

## Local LLM Requirement

The program requires a local OpenAI-compatible LLM API, such as LM Studio, an Ollama-compatible endpoint, or any server that supports `/v1/chat/completions`.

The default configuration is stored in:

```text
settings.json
```

Default fields:

```json
{
  "llm_base_url": "http://localhost:1234/v1",
  "llm_model": "gemma-4-26b-a4b-it",
  "llm_api_key": "lm-studio"
}
```

The preparation window saves the LLM settings, and the simulation window reuses the same configuration.

## Preparation Workflow

In the preparation UI:

1. Select a novel TXT file.
2. Set Story percentage.
3. Check Base URL, Model, and API key.
4. Click Check server to verify the local LLM.
5. Start preparation.

The preparation pipeline runs Steps 1-16 and publishes the final databases into `generated_db`.

Step 9 currently processes up to the first 30 chunks. If the selected source scope contains fewer than 30 chunks, it processes all available chunks in that scope.

## Output Folder Layout

The official output folder is:

```text
generated_db/
```

World databases:

```text
generated_db/world/
  novel_ontology.json
  raw_graph_triples.json
  mention_weak_relations.json
  normalized_graph_triples.json
  canonical_relationships_db.json
  relationship_arc_db.json
  structured_world_graph.json
  world_db.json
  canonical_novel_db.json
  simulation_state_db.json
  runtime_event_db.json
  simulation_state.json
```

Character databases:

```text
generated_db/characters/
  mention_alias_index.json
  canonical_entities.json
  character_state_db.json
```

Agent databases:

```text
generated_db/agents/
  agent_profiles.json
```

Compatibility copies may still exist in the root folder, but the simulation UI prefers the official files under `generated_db`.

## Layered World State System

Step 15 is the foundation of the simulation system. It no longer creates a single “novel summary database”; instead, it builds a layered world-state system.

### Canonical Novel DB

File:

```text
generated_db/world/canonical_novel_db.json
```

Purpose:

- Stores the read-only original trajectory.
- Stores character growth lines, relationship development lines, event chains, item flow, ability unlock paths, organization changes, and world rules.
- Serves as the large canonical save file and baseline.
- Does not directly define the current simulation state.

### Simulation State DB

File:

```text
generated_db/world/simulation_state_db.json
```

Purpose:

- Produces the current world checkpoint by cutting the Canonical Novel DB at a chosen time point.
- Contains only what has already happened, what is currently owned, what is currently known, and what has already been established.
- Abilities, items, identities, and relationships are not granted early just because they appear later in the original novel.

### Runtime Event DB

File:

```text
generated_db/world/runtime_event_db.json
```

Purpose:

- Stores future, waiting, active, completed, and blocked events.
- Canonical events are default pressure and reference tracks, not forced scripts.
- After the user diverges from canon, events may continue, change, be delayed, or be blocked.

## Ability, Item, Identity, and Acquisition System

Resources are managed through the Canonical Novel DB, Dependency Graph, and Acquisition System.

Core rules:

- Do not bind abilities, items, identities, or relationships directly to a character’s final result.
- Distinguish original canonical owners from current simulation owners.
- Distinguish exclusive resources from open/opportunity resources.
- Acquisition, loss, use, upgrade, and transfer must go through condition checks and event triggers.

Exclusive resource examples:

- Bloodline-locked resources.
- Martial soul or identity-locked resources.
- Resources only available to a specific character or qualified actor.

Open or opportunity resource examples:

- Whoever reaches a location.
- Whoever triggers an event.
- Whoever touches or obtains an item.
- Whoever satisfies organization, relationship, knowledge, or environment requirements.

Related data lives in:

```text
generated_db/world/canonical_novel_db.json
generated_db/world/world_db.json
```

Important sections include:

- `resources`
- `dependency_graph`
- `acquisition_system`
- acquisition conditions
- loss conditions
- use conditions
- upgrade conditions
- transfer conditions

## Relationship System

Relationship extraction has two stages.

### Mention-level Weak Relations

File:

```text
generated_db/world/mention_weak_relations.json
```

These are weak relationship signals extracted before Entity Resolution. They are resolver evidence, not final character relationships.

They include:

- Same-scene co-presence.
- Forms of address.
- Action links.
- Shared event participation.
- Shared location context.
- Shared item context.
- Explicit aliases.
- Titles.
- Transformation or form changes.

These weak links help the resolver decide whether mentions may refer to the same or related entities.

### Canonical Relationships

File:

```text
generated_db/world/canonical_relationships_db.json
```

After Entity Resolution is complete, weak relations are normalized to canonical entities and become canonical relationships.

### Relationship Arc DB

File:

```text
generated_db/world/relationship_arc_db.json
```

This database stores character-to-character relationship arcs for Agent social memory and runtime relationship tracking.

Relationships are not locked to their final canon outcome. Runtime relationship changes must be committed through events, such as:

- Shared experiences.
- Conflict.
- Rescue.
- Promise.
- Betrayal.
- Organization changes.
- User intervention.

## Running the Simulation

Run:

```bat
03_run_simulation.bat
```

The simulation UI reads:

```text
generated_db/world/world_db.json
generated_db/world/canonical_novel_db.json
generated_db/world/simulation_state_db.json
generated_db/world/runtime_event_db.json
generated_db/world/canonical_relationships_db.json
generated_db/world/relationship_arc_db.json
generated_db/characters/character_state_db.json
generated_db/agents/agent_profiles.json
```

Runtime state is saved to:

```text
generated_db/world/simulation_state.json
```

If the world DB fingerprint changes, the old `simulation_state.json` is backed up and a new runtime state is created to avoid mixing incompatible worlds.

## Simulation Agents

Agents make decisions from:

- The current Simulation State.
- The current Runtime Event Queue.
- The current scene.
- Visible character memories.
- Current relationship arc states.
- Currently acquired abilities, items, and identities.
- Current knowledge scope.
- World rules and evidence.

Agents should not act from the original novel’s final outcome.

## Diverging From Canon

The user can start from any character’s canonical anchor and freely diverge from the original story.

Examples:

- Refuse to trigger a canonical event.
- Leave a location early.
- Give an item to another character.
- Let a non-canonical owner attempt to obtain an open resource.
- Change character relationships.
- Delay, block, or rewrite an event.

The system preserves the canonical baseline, but runtime decisions use the current world state.

## Tests and Verification

Syntax check:

```bat
python -m py_compile relationship_state_layers.py db_output_layout.py world_state_layers.py step17_runtime.py pipeline_program.py app_files.py simulation_ui.py prepare_ui.py
```

Check whether simulation-required files exist:

```bat
python -c "from app_files import SIMULATION_REQUIRED_FILES, file_status; print([i['name'] for i in file_status(SIMULATION_REQUIRED_FILES) if not i['exists']])"
```

An empty list means the required simulation files are present.

## Main Python Files

```text
prepare_ui.py
```

Preparation UI.

```text
pipeline_program.py
```

Main Step 1-16 preparation pipeline.

```text
world_state_layers.py
```

Builds the layered world state, dependency graph, and acquisition system.

```text
relationship_state_layers.py
```

Builds mention weak relations, canonical relationships, and relationship arcs.

```text
db_output_layout.py
```

Publishes generated databases into `generated_db/world`, `generated_db/characters`, and `generated_db/agents`.

```text
step17_runtime.py
```

Simulation runtime, event commit system, branching state, World Validator, GM, world projection, and immersive scene generation.

```text
simulation_ui.py
```

Standalone simulation UI.

## Notes

- Step 15 is the simulation foundation. Changes to it must keep the layered DBs, resource system, relationship system, and runtime contracts consistent.
- Entity Resolution does not depend on final character relationships. It uses mention-level weak relations as evidence.
- Weak relations are not final relationships. Final relationships must be built after `canonical_entities.json` exists.
- Abilities, items, identities, and relationships should be driven by events and conditions, not automatically granted by chapter index.
- `generated_db/world/test_*.json` or `smoke_*.json` files are test state files, not required production files.
