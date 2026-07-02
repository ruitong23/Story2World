import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import type {
  Character,
  Dashboard,
  Relationship,
  WorldData,
} from "../api/types";
import { CharacterList } from "../components/CharacterList";
import { ErrorBox } from "../components/ErrorBox";
import { ArrowIcon, ChatIcon, GlobeIcon, UsersIcon } from "../components/Icons";
import { RelationshipTable } from "../components/RelationshipTable";
import { getProjectUsername } from "../lib/storage";
import { useLanguage } from "../i18n";

function sectionRows(
  world: WorldData | null,
  t: ReturnType<typeof useLanguage>["t"],
) {
  if (!world?.world_sections) return [];
  const labels: Record<string, string> = {
    locations: t("world.locations"),
    organizations: t("world.organizations"),
    events: t("world.events"),
    world_rules: t("world.rules"),
    abilities: t("world.abilities"),
    artifacts: t("world.artifacts"),
    knowledge_scopes: t("world.knowledge"),
  };
  return Object.entries(world.world_sections)
    .filter(([, value]) => Array.isArray(value) && value.length)
    .map(([key, value]) => ({
      key,
      label: labels[key] || key,
      values: value as Record<string, unknown>[],
    }));
}

function worldItemName(item: Record<string, unknown>, unnamed: string) {
  return String(
    item.canonical_name ||
      item.name ||
      item.display_name ||
      item.title ||
      item.entity_id ||
      unnamed,
  );
}

export function DashboardPage() {
  const { t } = useLanguage();
  const { projectId = "" } = useParams();
  const username = getProjectUsername(projectId);
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [characters, setCharacters] = useState<Character[]>([]);
  const [relationships, setRelationships] = useState<Relationship[]>([]);
  const [world, setWorld] = useState<WorldData | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState<"overview" | "relationships" | "world">("overview");

  useEffect(() => {
    if (!username) {
      setError(t("error.missingUsername"));
      setLoading(false);
      return;
    }
    Promise.all([
      api.getDashboard(projectId, username),
      api.getCharacters(projectId, username),
      api.getRelationships(projectId, username),
      api.getWorld(projectId, username),
    ])
      .then(([dashboardData, characterData, relationshipData, worldData]) => {
        setDashboard(dashboardData);
        setCharacters(characterData.characters || []);
        setRelationships(relationshipData.relationships || []);
        setWorld(worldData);
      })
      .catch((reason) =>
        setError(reason instanceof ApiError ? reason.message : t("error.loadWorld")),
      )
      .finally(() => setLoading(false));
  }, [projectId, t, username]);

  const sections = useMemo(() => sectionRows(world, t), [t, world]);
  const metrics = [
    [t("dashboard.characters"), dashboard?.characters_count, <UsersIcon />],
    [t("dashboard.agents"), dashboard?.agents_count, <ChatIcon />],
    [t("dashboard.locations"), dashboard?.locations_count, <GlobeIcon />],
    [t("dashboard.events"), dashboard?.events_count, <span className="metric-symbol">◎</span>],
    [t("dashboard.relationships"), dashboard?.relationships_count, <span className="metric-symbol">↔</span>],
  ] as const;

  if (loading) {
    return <div className="page-loader"><span /><p>{t("dashboard.loading")}</p></div>;
  }

  return (
    <div className="page-container dashboard-page">
      <header className="dashboard-hero">
        <div>
          <p className="eyebrow">WORLD DASHBOARD</p>
          <h1>{t("dashboard.title")}</h1>
          <p><code>{projectId}</code></p>
        </div>
        <Link className="button primary" to={`/projects/${projectId}/chat`}>
          <ChatIcon /> {t("dashboard.enter")} <ArrowIcon />
        </Link>
      </header>
      <ErrorBox message={error} />

      <section className="metric-grid dashboard-metrics">
        {metrics.map(([label, value, icon]) => (
          <article key={label}>
            <span className="metric-icon">{icon}</span>
            <div><small>{label}</small><strong>{value ?? "—"}</strong></div>
          </article>
        ))}
      </section>

      <div className="dashboard-tabs">
        {[
          ["overview", t("dashboard.overview")],
          ["relationships", t("dashboard.relationships")],
          ["world", t("dashboard.worldData")],
        ].map(([value, label]) => (
          <button
            key={value}
            className={activeTab === value ? "active" : ""}
            onClick={() => setActiveTab(value as typeof activeTab)}
          >
            {label}
          </button>
        ))}
      </div>

      {activeTab === "overview" && (
        <div className="dashboard-grid">
          <section className="panel main-panel">
            <div className="section-heading">
              <div><p className="eyebrow">CAST</p><h2>{t("dashboard.mainCharacters")}</h2></div>
              <span>{characters.filter((item) => item.available_as_agent).length} {t("dashboard.simulatable")}</span>
            </div>
            <CharacterList characters={characters.slice(0, 12)} />
          </section>
          <aside className="panel world-glance">
            <div className="section-heading">
              <div><p className="eyebrow">WORLD</p><h2>{t("dashboard.worldSlice")}</h2></div>
            </div>
            {sections.slice(0, 5).map((section) => (
              <div className="world-glance-row" key={section.key}>
                <span>{section.label}</span>
                <strong>{section.values.length}</strong>
                <p>
                  {section.values
                    .slice(0, 3)
                    .map((item) => worldItemName(item, t("world.unnamed")))
                    .join(" · ") || t("dashboard.none")}
                </p>
              </div>
            ))}
          </aside>
        </div>
      )}

      {activeTab === "relationships" && (
        <section className="panel">
          <div className="section-heading">
            <div><p className="eyebrow">RELATION GRAPH</p><h2>{t("dashboard.relationshipEvidence")}</h2></div>
            <span>{relationships.length} {t("dashboard.records")}</span>
          </div>
          <RelationshipTable relationships={relationships} />
        </section>
      )}

      {activeTab === "world" && (
        <div className="world-sections">
          {sections.map((section) => (
            <section className="panel" key={section.key}>
              <div className="section-heading">
                <h2>{section.label}</h2>
                <span>{section.values.length}</span>
              </div>
              <div className="tag-cloud">
                {section.values.slice(0, 30).map((item, index) => (
                  <span key={`${worldItemName(item, t("world.unnamed"))}-${index}`}>
                    {worldItemName(item, t("world.unnamed"))}
                  </span>
                ))}
              </div>
            </section>
          ))}
          {!sections.length && <div className="empty-state">{t("dashboard.noWorldData")}</div>}
        </div>
      )}
    </div>
  );
}
