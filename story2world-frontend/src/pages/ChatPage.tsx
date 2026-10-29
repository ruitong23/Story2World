import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import type {
  AgentActivity,
  AgentTrace,
  AgentTraceActor,
  AgentTraceContributor,
  AnchorPreviewResponse,
  Character,
  CharacterRuntimeState,
  ChatProgress,
  ChatResponse,
  RecoverySnapshot,
  StoryProgress,
} from "../api/types";
import { CharacterList } from "../components/CharacterList";
import { ErrorBox } from "../components/ErrorBox";
import {
  ArrowIcon,
  ChatIcon,
  HeartIcon,
  ShieldIcon,
  SparkIcon,
  TargetIcon,
} from "../components/Icons";
import { getProjectUsername, getSessionId } from "../lib/storage";
import { useLanguage } from "../i18n";

interface Message {
  role: "user" | "assistant";
  content: string;
  details?: ChatResponse;
  recovery?: boolean;
}

function DetailList({ title, values }: { title: string; values?: unknown[] }) {
  if (!values?.length) return null;
  return (
    <details className="chat-details">
      <summary>{title} · {values.length}</summary>
      <div>
        {values.slice(0, 12).map((item, index) => (
          <pre key={index}>{JSON.stringify(item, null, 2)}</pre>
        ))}
      </div>
    </details>
  );
}

function runtimeText(value: unknown, fallback = "—") {
  if (typeof value === "string" && value.trim()) return value;
  return fallback;
}

type Translate = ReturnType<typeof useLanguage>["t"];

function recoveryText(snapshot: RecoverySnapshot | undefined, t: Translate) {
  if (!snapshot?.summary) return "";
  const nearby = snapshot.nearby_state || {};
  const names = (nearby.characters || [])
    .map((item) => item.name)
    .filter(Boolean)
    .join(", ");
  const clock = nearby.clock || {};
  const minute = Number(clock.minute_of_day ?? 480);
  const stateLines = [
    `${t("chat.location")}: ${nearby.location_name || nearby.scene_summary || t("chat.currentLocation")}`,
    `${t("chat.nearby")}: ${names || t("chat.noNearby")}`,
    `${t("chat.time")}: ${t("chat.dayTime", {
      day: clock.day || 1,
      time: `${String(Math.floor(minute / 60)).padStart(2, "0")}:${String(minute % 60).padStart(2, "0")}`,
    })}`,
  ];
  return `${t("chat.restoreTitle")}\n\n${snapshot.summary}\n\n${stateLines.join(" · ")}`;
}

function localizeProgress(
  update: ChatProgress | null,
  t: Translate,
) {
  if (!update) return null;
  const label = update.label || "";
  if (update.progress >= 100) {
    return { ...update, action: t("chat.stageComplete"), detail: t("chat.detailSave") };
  }
  if (label.includes("时间 Agent")) {
    return { ...update, actor: "Time Agent", action: t("chat.stageTime"), detail: t("chat.detailTime") };
  }
  if (label.includes("局部世界")) {
    return { ...update, actor: "Local World Agent", action: t("chat.stageLocal"), detail: t("chat.detailLocal") };
  }
  if (label.includes("Group Controller") || label.includes("群体")) {
    return { ...update, actor: "Group Controller", action: t("chat.stageGroup"), detail: t("chat.detailGroup") };
  }
  if (label.includes("GM")) {
    return { ...update, actor: "GM Resolver", action: t("chat.stageGm"), detail: t("chat.detailGm") };
  }
  if (label.includes("Renderer") || label.includes("写作")) {
    return { ...update, actor: "Scene Renderer", action: t("chat.stageRender"), detail: t("chat.detailRender") };
  }
  if (label.includes("大世界")) {
    return { ...update, actor: "World Agent", action: t("chat.stageWorld"), detail: t("chat.detailWorld") };
  }
  if (label.includes("保存") || label.includes("同步") || label.includes("存档")) {
    return { ...update, actor: "Save System", action: t("chat.stageSave"), detail: t("chat.detailSave") };
  }
  if (label.includes("正在观察并行动")) {
    return { ...update, action: t("chat.stageNpc"), detail: t("chat.detailNpc") };
  }
  if (label.includes("附近")) {
    return { ...update, actor: "Nearby Agents", action: t("chat.stageNearby"), detail: t("chat.detailNearby") };
  }
  if (label.includes("角色")) {
    return { ...update, actor: "Player Agent", action: t("chat.stagePlayer"), detail: t("chat.detailPlayer") };
  }
  return update;
}

function agentLabel(
  item: AgentTraceActor | AgentTraceContributor,
  fallback: string,
) {
  return item.name || ("character_id" in item ? item.character_id : "") || fallback;
}

function AgentTracePanel({ trace }: { trace: AgentTrace | null }) {
  const { t } = useLanguage();
  const controlled = trace?.active_controlled_agents?.length
    ? trace.active_controlled_agents
    : trace?.active_full_agents || [];
  const passive = trace?.active_passive_characters?.length
    ? trace.active_passive_characters
    : trace?.active_other_agents || [];
  const contributors = trace?.turn_contributors || [];
  const hasTrace = controlled.length || passive.length || contributors.length;

  if (!trace && !hasTrace) return null;

  const renderAgent = (item: AgentTraceActor, index: number) => {
    const meta = [
      item.tier,
      item.control,
      item.runtime_mode,
      item.agent_awake ? t("chat.agentAwake") : "",
    ].filter(Boolean);
    return (
      <div className="agent-trace-row" key={`${item.character_id || item.name || "agent"}-${index}`}>
        <strong>{agentLabel(item, t("chat.character"))}</strong>
        <span>{meta.join(" / ") || t("chat.agentPresent")}</span>
      </div>
    );
  };

  return (
    <section className="status-section agent-trace-section">
      <h3><ChatIcon />{t("chat.agentTrace")}</h3>
      {!hasTrace && <small className="agent-trace-empty">{t("chat.noAgentTrace")}</small>}
      {controlled.length > 0 && (
        <div className="agent-trace-block">
          <h4>{t("chat.agentControlled")}</h4>
          <div className="agent-trace-list">{controlled.map(renderAgent)}</div>
        </div>
      )}
      {passive.length > 0 && (
        <div className="agent-trace-block">
          <h4>{t("chat.agentPassive")}</h4>
          <div className="agent-trace-list">{passive.map(renderAgent)}</div>
        </div>
      )}
      {contributors.length > 0 && (
        <div className="agent-trace-block">
          <h4>{t("chat.agentContributors")}</h4>
          <div className="agent-trace-list">
            {contributors.map((item, index) => (
              <div className="agent-trace-row" key={`${item.name || "contributor"}-${index}`}>
                <strong>{agentLabel(item, t("chat.world"))}</strong>
                <span>{[item.kind, item.status].filter(Boolean).join(" / ") || t("chat.completedAction")}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}

function CharacterStatusPanel({
  character,
  state,
  activities,
  agentTrace,
}: {
  character: Character | null;
  state: CharacterRuntimeState;
  activities: AgentActivity[];
  agentTrace: AgentTrace | null;
}) {
  const { t } = useLanguage();
  const health = state.health || { current: 100, maximum: 100, status: t("chat.healthy") };
  const current = health.current ?? 100;
  const maximum = health.maximum || 100;
  const healthPercent = Math.max(0, Math.min(100, (current / maximum) * 100));
  const equipment = [
    ...(state.equipment || []),
    ...(state.held_items || []),
    ...(character?.items || []).map((item) => item.name || "").filter(Boolean),
  ].filter((item, index, values) => item && values.indexOf(item) === index);

  return (
    <aside className="status-sidebar">
      <div className="status-profile">
        <div className="avatar status-avatar">{(character?.name || "?").slice(0, 1)}</div>
        <div>
          <p className="eyebrow">CHARACTER STATUS</p>
          <h2>{character?.name || t("chat.notSelected")}</h2>
          <span>{character?.tier || "agent"}</span>
        </div>
      </div>

      <section className="vital-card">
        <div className="vital-label">
          <span><HeartIcon />{t("chat.health")}</span>
          <strong>{current} / {maximum}</strong>
        </div>
        <div className="health-track"><i style={{ width: `${healthPercent}%` }} /></div>
        <small>{health.status || t("chat.healthy")}</small>
      </section>

      <section className="status-section">
        <h3><TargetIcon />{t("chat.currentStatus")}</h3>
        <dl className="status-grid">
          <div><dt>{t("chat.posture")}</dt><dd>{runtimeText(state.posture, t("chat.natural"))}</dd></div>
          <div><dt>{t("chat.mood")}</dt><dd>{runtimeText(state.mood, t("chat.calm"))}</dd></div>
          <div className="wide"><dt>{t("chat.activity")}</dt><dd>{runtimeText(state.current_activity, t("chat.observing"))}</dd></div>
          <div className="wide"><dt>{t("chat.goal")}</dt><dd>{runtimeText(state.short_term_goal, t("chat.continueLife"))}</dd></div>
        </dl>
      </section>

      <section className="status-section">
        <h3><ShieldIcon />{t("chat.equipment")}</h3>
        <div className="status-tags">
          {equipment.length
            ? equipment.map((item) => <span key={item}>{item}</span>)
            : <small>{t("chat.noEquipment")}</small>}
        </div>
      </section>

      <section className="status-section">
        <h3><SparkIcon />{t("chat.abilities")}</h3>
        <div className="status-tags ability-tags">
          {character?.abilities?.length
            ? character.abilities.map((item) => (
                <span key={item.entity_id || item.name}>{item.name || t("chat.unnamedAbility")}</span>
              ))
            : <small>{t("chat.noAbilities")}</small>}
        </div>
      </section>

      {activities.length > 0 && (
        <section className="status-section activity-history">
          <h3><ChatIcon />{t("chat.recentActions")}</h3>
          {activities.slice(-4).map((item, index) => (
            <div key={`${item.agent}-${index}`}>
              <strong>{item.agent || t("chat.world")}</strong>
              <p>{item.activity || t("chat.completedAction")}</p>
            </div>
          ))}
        </section>
      )}

      <AgentTracePanel trace={agentTrace} />
    </aside>
  );
}

export function ChatPage() {
  const { t } = useLanguage();
  const { projectId = "" } = useParams();
  const username = getProjectUsername(projectId);
  const [characters, setCharacters] = useState<Character[]>([]);
  const [selected, setSelected] = useState<Character | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveNotice, setSaveNotice] = useState("");
  const [elapsed, setElapsed] = useState(0);
  const [progress, setProgress] = useState<ChatProgress | null>(null);
  const [progressEvents, setProgressEvents] = useState<ChatProgress[]>([]);
  const [runtimeState, setRuntimeState] = useState<CharacterRuntimeState>({});
  const [activities, setActivities] = useState<AgentActivity[]>([]);
  const [agentTrace, setAgentTrace] = useState<AgentTrace | null>(null);
  const [anchorPreview, setAnchorPreview] = useState<AnchorPreviewResponse | null>(null);
  const [previewingAnchor, setPreviewingAnchor] = useState(false);
  const [storyProgress, setStoryProgress] = useState<StoryProgress | null>(null);

  useEffect(() => {
    if (!username) {
      setError(t("error.missingUsername"));
      setLoading(false);
      return;
    }
    api
      .getCharacters(projectId, username)
      .then((result) => {
        const agents = (result.characters || []).filter(
          (item) => item.available_as_agent,
        ).sort((left, right) => {
          const tier = (value?: string) =>
            value === "full" ? 4 : value === "light" ? 3 : value === "reference" ? 2 : 1;
          const score = (item: Character) =>
            tier(item.tier) * 1000 +
            (item.relationship_count || 0) * 20 +
            (item.ability_count || 0) * 15 +
            (item.item_count || 0) * 10 +
            (item.aliases?.length || 0) +
            (item.titles?.length || 0);
          return score(right) - score(left) || (left.name || "").localeCompare(right.name || "");
        });
        setCharacters(agents);
        setSelected(agents[0] || null);
      })
      .catch((reason) =>
        setError(reason instanceof ApiError ? reason.message : t("error.readCharacters")),
      )
      .finally(() => setLoading(false));
  }, [projectId, t, username]);

  useEffect(() => {
    if (!sending) return;
    setElapsed(0);
    const timer = window.setInterval(() => setElapsed((value) => value + 1), 1000);
    return () => window.clearInterval(timer);
  }, [sending]);

  const sessionId = useMemo(
    () => (selected ? getSessionId(projectId, selected.character_id) : ""),
    [projectId, selected],
  );

  useEffect(() => {
    if (!selected || !username || !sessionId) return;
    let cancelled = false;
    setMessages([]);
    setRuntimeState({});
    setActivities([]);
    setAgentTrace(null);
    setAnchorPreview(null);
    setStoryProgress(null);
    setSaveNotice("");
    api
      .getChatSession(projectId, {
        username,
        sessionId,
        characterId: selected.character_id,
      })
      .then((result) => {
        if (cancelled) return;
        setRuntimeState(result.character_state || {});
        setAgentTrace(result.agent_trace || null);
        setStoryProgress(result.story_progress || null);
        const restored = recoveryText(result.recovery_snapshot, t);
        if (restored) {
          setMessages([
            { role: "assistant", content: restored, recovery: true },
          ]);
        }
      })
      .catch((reason) => {
        if (!cancelled) {
          setError(
            reason instanceof ApiError
              ? reason.message
              : t("error.restoreSession"),
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, selected, sessionId, t, username]);

  const selectCharacter = (character: Character) => {
    setSelected(character);
    setAnchorPreview(null);
    setError("");
  };

  const previewAnchor = async () => {
    if (!selected || !username || !sessionId || previewingAnchor) return;
    setPreviewingAnchor(true);
    setError("");
    try {
      const result = await api.getAnchorPreview(projectId, {
        username,
        sessionId,
        characterId: selected.character_id,
      });
      setAnchorPreview(result);
    } catch (reason) {
      setError(
        reason instanceof ApiError ? reason.message : t("error.anchorPreview"),
      );
    } finally {
      setPreviewingAnchor(false);
    }
  };

  const send = async () => {
    const text = input.trim();
    if (!text || !selected || sending) return;
    setMessages((current) => [...current, { role: "user", content: text }]);
    setInput("");
    setSending(true);
    setProgress(null);
    setProgressEvents([]);
    setError("");
    try {
      const result = await api.chatStream(
        projectId,
        {
          username,
          sessionId,
          characterId: selected.character_id,
          message: text,
        },
        (update) => {
          setProgress(update);
          setProgressEvents((current) => {
            const previous = current[current.length - 1];
            if (
              previous?.label === update.label &&
              previous?.progress === update.progress
            ) {
              return current;
            }
            return [...current, update].slice(-10);
          });
        },
      );
      setRuntimeState(result.character_state || {});
      setStoryProgress(result.story_progress || null);
      setActivities(result.agent_activity || []);
      setAgentTrace(result.agent_trace || null);
      setMessages((current) => [
        ...current,
        { role: "assistant", content: result.reply, details: result },
      ]);
    } catch (reason) {
      setError(
        reason instanceof ApiError
          ? reason.message
          : t("error.worldOffline"),
      );
    } finally {
      setSending(false);
    }
  };

  const save = async () => {
    if (!selected || saving || sending) return;
    setSaving(true);
    setSaveNotice("");
    setError("");
    try {
      const result = await api.saveChatSession(projectId, {
        username,
        sessionId,
        characterId: selected.character_id,
      });
      const restored = recoveryText(result.recovery_snapshot, t);
      setSaveNotice(t("chat.saved"));
      if (restored) {
        setMessages((current) => {
          const withoutOldRecovery = current.filter(
            (item) => !item.recovery,
          );
          return [
            { role: "assistant", content: restored, recovery: true },
            ...withoutOldRecovery,
          ];
        });
      }
    } catch (reason) {
      setError(
        reason instanceof ApiError ? reason.message : t("error.save"),
      );
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return <div className="page-loader"><span /><p>{t("chat.loadingCharacters")}</p></div>;
  }

  const visibleProgress = localizeProgress(progress, t);

  return (
    <div className="chat-page">
      <aside className="chat-sidebar">
        <div className="chat-sidebar-head">
          <Link to={`/projects/${projectId}`}>{t("chat.back")}</Link>
          <p className="eyebrow">{t("chat.choose")}</p>
          <h2>{t("chat.chooseTitle")}</h2>
        </div>
        <CharacterList
          characters={characters}
          selectedId={selected?.character_id}
          onSelect={selectCharacter}
        />
      </aside>

      <section className="chat-stage">
        <header className="chat-header">
          <div className="avatar large">{(selected?.name || "?").slice(0, 1)}</div>
          <div>
            <p className="eyebrow">{t("chat.active")}</p>
            <h1>{selected?.name || t("chat.noCharacter")}</h1>
            <span>{selected?.short_description || t("chat.selectToStart")}</span>
          </div>
          <div className="chat-header-actions">
            <button
              className="button ghost save-session-button"
              onClick={previewAnchor}
              disabled={!selected || sending || saving || previewingAnchor}
            >
              {previewingAnchor ? t("chat.previewingAnchor") : t("chat.previewAnchor")}
            </button>
            <button
              className="button ghost save-session-button"
              onClick={save}
              disabled={!selected || sending || saving}
            >
              {saving ? t("chat.saving") : t("chat.save")}
            </button>
            <code>{sessionId}</code>
          </div>
        </header>

        <div className="chat-messages">
          {(anchorPreview || storyProgress) && (
            <section className="anchor-preview-card">
              {storyProgress && (
                <div className="story-progress-line">
                  <strong>{t("chat.storyProgress")}</strong>
                  <span>
                    {Math.round(storyProgress.canonical_percent || 0)}%
                    {" · "}
                    {storyProgress.canonical_reached_events || 0}
                    /
                    {storyProgress.canonical_total_events || 0}
                    {" · "}
                    {storyProgress.runtime_event_count || 0} runtime events
                  </span>
                </div>
              )}
              {anchorPreview && (
                <>
                  <h2>{t("chat.anchorPreviewTitle")}</h2>
                  <p>{anchorPreview.summary}</p>
                  <details>
                    <summary>{t("chat.sources")}</summary>
                    <pre>{anchorPreview.preview_material}</pre>
                  </details>
                </>
              )}
            </section>
          )}
          {!messages.length && selected && !sending && (
            <div className="chat-empty">
              <span><SparkIcon /></span>
              <h2>{t("chat.emptyTitle")}</h2>
              <p>{t("chat.emptyText")}</p>
              <div>
                {t("chat.prompts").split("|").map(
                  (prompt) => (
                    <button key={prompt} onClick={() => setInput(prompt)}>
                      {prompt}
                    </button>
                  ),
                )}
              </div>
            </div>
          )}
          {messages.map((message, index) => (
            <article className={`message ${message.role} ${message.recovery ? "recovery" : ""}`} key={index}>
              <span className="message-role">
                {message.role === "user"
                  ? t("chat.you")
                  : message.recovery
                    ? t("chat.restore")
                    : selected?.name || t("chat.character")}
              </span>
              <div className="message-body">
                <p>{message.content}</p>
                {message.details && (
                  <div className="message-details">
                    <DetailList title={t("chat.sources")} values={message.details.used_sources} />
                    <DetailList title={t("chat.constraints")} values={message.details.world_constraints} />
                    <DetailList title={t("chat.related")} values={message.details.related_relationships} />
                  </div>
                )}
              </div>
            </article>
          ))}
          {sending && (
            <section className="agent-process-card" aria-live="polite">
              <div className="agent-process-head">
                <span className="process-orb"><i /></span>
                <div>
                  <p className="eyebrow">{t("chat.live")}</p>
                  <h2>{visibleProgress?.actor || t("chat.worldService")}</h2>
                  <strong>{visibleProgress?.action || t("chat.starting")}</strong>
                  <span>{visibleProgress?.detail || t("chat.waitingStage")}</span>
                </div>
                <em>{elapsed}s</em>
              </div>
              <div className="agent-progress">
                <i style={{ width: `${progress?.progress || 1}%` }} />
              </div>
              <div className="agent-progress-note">
                <span>{visibleProgress?.action || t("chat.connecting")}</span>
                <small>{t("chat.backendStage", { progress: progress?.progress || 0 })}</small>
              </div>
              <div className="agent-timeline">
                {progressEvents.map((phase, index) => (
                  <div
                    className={
                      index < progressEvents.length - 1
                        ? "complete"
                        : index === progressEvents.length - 1
                          ? "active"
                          : ""
                    }
                    key={`${phase.progress}-${phase.label}-${index}`}
                  >
                    <i />
                    <span>
                      <strong>{localizeProgress(phase, t)?.actor}</strong>
                      <small>{localizeProgress(phase, t)?.action}</small>
                    </span>
                  </div>
                ))}
              </div>
            </section>
          )}
        </div>

        <div className="chat-composer">
          <ErrorBox message={error} />
          {saveNotice && <p className="save-notice">{saveNotice}</p>}
          <div>
            <textarea
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void send();
                }
              }}
              placeholder={
                selected
                  ? t("chat.placeholder", { name: selected.name || t("chat.character") })
                  : t("chat.noAvailable")
              }
              disabled={!selected || sending}
              rows={3}
            />
            <button
              className="button primary send-button"
              onClick={send}
              disabled={!selected || !input.trim() || sending}
              aria-label={t("chat.send")}
            >
              <ChatIcon /><ArrowIcon />
            </button>
          </div>
          <small>{t("chat.hint")}</small>
        </div>
      </section>

      <CharacterStatusPanel
        character={selected}
        state={runtimeState}
        activities={activities}
        agentTrace={agentTrace}
      />
    </div>
  );
}
