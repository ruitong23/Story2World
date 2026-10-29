import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, ApiError } from "../api/client";
import type { UserProject } from "../api/types";
import { ErrorBox } from "../components/ErrorBox";
import {
  ArrowIcon,
  FolderIcon,
  SparkIcon,
  UploadIcon,
} from "../components/Icons";
import {
  getStoredUsername,
  rememberProject,
  setStoredUsername,
} from "../lib/storage";
import { LanguageSwitcher, useLanguage } from "../i18n";

export function HomePage() {
  const { language, t } = useLanguage();
  const navigate = useNavigate();
  const [username, setUsername] = useState(getStoredUsername());
  const [projectId, setProjectId] = useState("");
  const [error, setError] = useState("");
  const [projects, setProjects] = useState<UserProject[]>([]);
  const [searching, setSearching] = useState(false);

  const openProject = () => {
    if (!username.trim() || !projectId.trim()) {
      setError(t("error.enterUserProject"));
      return;
    }
    setStoredUsername(username);
    rememberProject(projectId, username);
    navigate(`/projects/${encodeURIComponent(projectId.trim())}`);
  };

  const findProjects = async () => {
    if (!username.trim()) {
      setError(t("error.enterUsername"));
      return;
    }
    setSearching(true);
    setError("");
    try {
      setStoredUsername(username);
      const result = await api.getUserProjects(username.trim());
      setProjects(result.projects || []);
      if (!result.projects?.length) setError(t("error.noProjects"));
    } catch (reason) {
      setError(
        reason instanceof ApiError ? reason.message : t("error.readProjects"),
      );
    } finally {
      setSearching(false);
    }
  };

  const openListedProject = (item: UserProject) => {
    if (!item.project_id) return;
    rememberProject(item.project_id, username);
    navigate(`/projects/${encodeURIComponent(item.project_id)}`);
  };

  return (
    <div className="home-page">
      <section className="hero">
        <div className="hero-glow" />
        <div className="hero-content">
          <LanguageSwitcher prominent />
          <p className="eyebrow">
            <SparkIcon /> {t("home.eyebrow")}
          </p>
          <h1>
            {t("home.title1")}
            <br />
            <span>{t("home.title2")}</span>
          </h1>
          <p className="hero-lead">
            NavelMaker 2 · Story2World
          </p>
          <p className="hero-description">
            {t("home.description")}
          </p>
          <div className="hero-meta">
            <span><i />{t("home.metaModel")}</span>
            <span><i />{t("home.metaAgents")}</span>
            <span><i />{t("home.metaWorld")}</span>
          </div>
          <div className="hero-actions">
            <Link className="button primary large" to="/projects/new">
              <UploadIcon />
              {t("home.process")}
              <ArrowIcon />
            </Link>
            <a className="button ghost large" href="#existing-project">
              <FolderIcon />
              {t("home.openExisting")}
            </a>
          </div>
        </div>
        <div className="hero-visual" aria-hidden="true">
          <div className="model-crown">
            <SparkIcon />
            <span>AUTHOR MODEL</span>
          </div>
          <div className="orbit orbit-one" />
          <div className="orbit orbit-two" />
          <div className="world-core">
            <small>STORY WORLD</small>
            <span>{t("home.world")}</span>
            <strong>ONLINE</strong>
          </div>
          {t("home.nodes").split("|").map((label, index) => (
            <span className={`world-node node-${index + 1}`} key={label}>
              {label}
            </span>
          ))}
          <div className="simulation-preview">
            <span className="preview-avatar">{language === "zh" ? "唐" : "A"}</span>
            <div>
              <small>ACTIVE AGENT</small>
              <strong>{t("home.agentWorking")}</strong>
              <i><b /></i>
            </div>
          </div>
        </div>
      </section>

      <section className="feature-strip">
        <article>
          <span>01</span>
          <strong>{t("home.feature1")}</strong>
          <p>{t("home.feature1Text")}</p>
        </article>
        <article>
          <span>02</span>
          <strong>{t("home.feature2")}</strong>
          <p>{t("home.feature2Text")}</p>
        </article>
        <article>
          <span>03</span>
          <strong>{t("home.feature3")}</strong>
          <p>{t("home.feature3Text")}</p>
        </article>
      </section>

      <section className="existing-project" id="existing-project">
        <div>
          <p className="eyebrow">RESUME A WORLD</p>
          <h2>{t("home.resumeTitle")}</h2>
          <p className="muted">
            {t("home.resumeText")}
          </p>
        </div>
        <div className="resume-form">
          <label className="field">
            <span>{t("common.username")}</span>
            <input
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              placeholder={t("home.usernamePlaceholder")}
            />
          </label>
          <label className="field">
            <span>{t("common.projectId")}</span>
            <input
              value={projectId}
              onChange={(event) => setProjectId(event.target.value)}
              placeholder="project_xxxxxxxxxxxxxxxx"
            />
          </label>
          <button
            className="button ghost"
            onClick={findProjects}
            disabled={searching}
          >
            {searching ? t("home.finding") : t("home.findProjects")}
          </button>
          <ErrorBox message={error} />
          {projects.length > 0 && (
            <div className="saved-projects">
              {projects.slice(0, 8).map((item) => (
                <button
                  key={item.project_id}
                  onClick={() => openListedProject(item)}
                >
                  <span>
                    <strong>{item.original_filename || item.project_id}</strong>
                    <small>{item.message || item.status || t("common.unknown")}</small>
                  </span>
                  <em>{item.status || "—"}</em>
                  <ArrowIcon />
                </button>
              ))}
            </div>
          )}
          <button className="button secondary" onClick={openProject}>
            {t("home.openWorld")} <ArrowIcon />
          </button>
        </div>
      </section>
    </div>
  );
}
