export type ProjectStatusName =
  | "uploaded"
  | "queued"
  | "processing"
  | "ready"
  | "failed"
  | string;

export interface EstimateResponse {
  character_count?: number;
  section_count?: number;
  split_method?: string;
  chunk_size?: number;
  overlap?: number;
  estimated_total_chunks?: number;
  seconds_per_chunk?: number;
  estimated_pipeline_overhead_seconds?: number;
  estimated_full_seconds?: number;
  estimated_full_text?: string;
  note?: string;
}

export interface SourcePreviewResponse {
  percent: number;
  character_range?: number[];
  summary: string;
  excerpt: string;
}

export interface LLMProfile {
  profile_name: string;
  llm_base_url: string;
  llm_model: string;
  llm_api_key: string;
}

export interface LLMProfilesResponse {
  active_llm_profile: string;
  profiles: LLMProfile[];
  env_override?: Record<string, boolean>;
}

export interface LLMCheckResponse {
  ok: boolean;
  active_llm_profile?: string;
  selected_model?: string;
  selected_model_found?: boolean;
  models?: string[];
}

export interface TokenUsageBucket {
  name: string;
  call_count: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

export interface TokenUsageRecord {
  timestamp?: string;
  source?: string;
  flow?: string;
  model: string;
  base_url_host?: string;
  call_count?: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  estimated?: boolean;
}

export interface TokenUsageResponse {
  totals: {
    call_count: number;
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
  };
  by_source: TokenUsageBucket[];
  by_model: TokenUsageBucket[];
  recent: TokenUsageRecord[];
  path?: string;
}

export interface CreateProjectResponse extends EstimateResponse {
  username: string;
  project_id: string;
  status: ProjectStatusName;
  selected_chunks?: number;
  estimated_selected_seconds?: number;
  estimated_selected_text?: string;
}

export interface PipelineQuality {
  valid_chunk_count?: number;
  partial_chunk_count?: number;
  validation_error_count?: number;
  node_count?: number;
  edge_count?: number;
}

export interface ProjectStatus {
  project_id: string;
  username?: string;
  status: ProjectStatusName;
  current_step?: string;
  progress?: number;
  message?: string;
  error?: string | null;
  current_chunk?: number | null;
  processing_chunk_total?: number | null;
  current_batch?: number | null;
  processing_batch_total?: number | null;
  elapsed_seconds?: number;
  estimated_remaining_seconds?: number;
  estimated_remaining_text?: string;
  selected_chunks?: number;
  estimated_total_chunks?: number;
  pipeline_quality?: PipelineQuality;
  warnings?: string[];
}

export interface MainCharacter {
  character_id?: string;
  name?: string;
  aliases?: string[];
  description?: string;
  tier?: string;
}

export interface Dashboard {
  project_id: string;
  status: string;
  characters_count?: number;
  agents_count?: number;
  locations_count?: number;
  events_count?: number;
  relationships_count?: number;
  main_characters?: MainCharacter[];
  current_world_progress?: Record<string, unknown>;
  available_features?: string[];
  runtime_capabilities?: Record<string, boolean>;
}

export interface Character {
  character_id: string;
  name?: string;
  aliases?: string[];
  titles?: string[];
  short_description?: string;
  available_as_agent?: boolean;
  has_prebuilt_agent_profile?: boolean;
  relationship_count?: number;
  ability_count?: number;
  item_count?: number;
  abilities?: Array<{
    entity_id?: string;
    name?: string;
    relation_type?: string;
  }>;
  items?: Array<{
    entity_id?: string;
    name?: string;
    relation_type?: string;
  }>;
  tier?: string;
}

export interface CharactersResponse {
  project_id: string;
  characters: Character[];
}

export interface Relationship {
  source_character?: string;
  relationship_type?: string;
  target_character?: string;
  description?: string;
  confidence?: string | number;
  source_event?: string;
  source_text?: string;
  source_chunk_id?: number;
  canonical_source_id?: string;
  canonical_target_id?: string;
}

export interface RelationshipsResponse {
  project_id: string;
  relationships: Relationship[];
}

export interface WorldData {
  world_sections?: Record<string, unknown[]>;
  knowledge_units?: unknown[];
  validation?: Record<string, unknown>;
  project_language?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface ChatResponse {
  session_id: string;
  character_id: string;
  reply: string;
  used_sources?: unknown[];
  world_constraints?: unknown[];
  related_relationships?: Relationship[];
  state_delta?: Record<string, unknown>;
  character_state?: CharacterRuntimeState;
  scene_state?: Record<string, unknown>;
  agent_activity?: AgentActivity[];
  agent_trace?: AgentTrace;
  recovery_snapshot?: RecoverySnapshot;
  rag_orchestration_summary?: Record<string, unknown>;
  story_progress?: StoryProgress;
}

export interface ChatProgress {
  progress: number;
  label: string;
  actor: string;
  action: string;
  detail: string;
  status?: string;
}

export interface RecoverySnapshot {
  saved_at?: string;
  revision?: number;
  focus_character_id?: string;
  summary?: string;
  nearby_state?: {
    location_id?: string | null;
    location_name?: string;
    scene_summary?: string;
    characters?: Array<{
      character_id?: string;
      name?: string;
      activity?: string;
      posture?: string;
      mood?: string;
      availability?: string;
    }>;
    sensory_environment?: Record<string, unknown>;
    active_events?: unknown[];
    clock?: {
      day?: number;
      minute_of_day?: number;
      [key: string]: unknown;
    };
  };
  recent_turn_count?: number;
}

export interface ChatSessionResponse {
  session_id: string;
  character_id: string;
  has_session: boolean;
  recovery_snapshot?: RecoverySnapshot;
  scene_state?: Record<string, unknown>;
  character_state?: CharacterRuntimeState;
  agent_trace?: AgentTrace;
  state_revision?: number;
  story_progress?: StoryProgress;
}

export interface ChatSaveResponse {
  session_id: string;
  character_id: string;
  saved: boolean;
  recovery_snapshot?: RecoverySnapshot;
  state_revision?: number;
}

export interface CharacterRuntimeState {
  health?: {
    current?: number;
    maximum?: number;
    status?: string;
  };
  current_location?: string | null;
  posture?: string;
  current_activity?: string;
  held_items?: string[];
  equipment?: string[];
  clothing?: string;
  mood?: string;
  attention_target?: string;
  short_term_goal?: string;
  long_term_goal?: string;
  physical_state?: string;
  availability?: string;
  visible_injuries?: string[];
  active_effects?: string[];
  [key: string]: unknown;
}

export interface AgentActivity {
  agent?: string;
  activity?: string;
  emotion?: string;
  status?: string;
}

export interface AgentTraceActor {
  character_id?: string;
  name?: string;
  tier?: string;
  control?: string;
  runtime_mode?: string;
  agent_awake?: boolean;
}

export interface AgentTraceContributor {
  name?: string;
  kind?: string;
  status?: string;
}

export interface AgentTrace {
  active_controlled_agents?: AgentTraceActor[];
  active_passive_characters?: AgentTraceActor[];
  active_full_agents?: AgentTraceActor[];
  active_other_agents?: AgentTraceActor[];
  turn_contributors?: AgentTraceContributor[];
  scene?: Record<string, unknown>;
  event_id?: string;
  revision?: string | number;
  pipeline_summary?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface StoryProgress {
  timeline_cursor?: number;
  canonical_reached_events?: number;
  canonical_total_events?: number;
  canonical_percent?: number;
  runtime_event_count?: number;
  sidecar_committed_event_count?: number;
}

export interface AnchorPreviewResponse {
  project_id: string;
  session_id: string;
  character_id: string;
  summary: string;
  preview_material: string;
  preview_cursor?: number;
  current_anchor?: Record<string, unknown>;
  focus_character_packet?: Record<string, unknown>;
}

export interface UserProject {
  project_id?: string;
  username?: string;
  original_filename?: string;
  created_at?: string;
  status?: string;
  progress?: number;
  message?: string;
}

export interface UserProjectsResponse {
  username: string;
  projects: UserProject[];
}
