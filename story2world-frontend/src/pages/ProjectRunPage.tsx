import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import type { ProjectStatus } from "../api/types";
import { ErrorBox } from "../components/ErrorBox";
import { ArrowIcon, CheckIcon, SparkIcon } from "../components/Icons";
import { ProgressBar } from "../components/ProgressBar";
import { StatusBadge } from "../components/StatusBadge";
import { getProjectUsername } from "../lib/storage";
import { useLanguage } from "../i18n";

export function ProjectRunPage() {
  const { t } = useLanguage();
  const { projectId = "" } = useParams();
  const username = getProjectUsername(projectId);
  const [status, setStatus] = useState<ProjectStatus | null>(null);
  const [error, setError] = useState("");
  const [starting, setStarting] = useState(false);

  const loadStatus = useCallback(async () => {
    if (!username) {
      setError(t("error.projectUsername"));
      return;
    }
    try {
      setStatus(await api.getStatus(projectId, username));
      setError("");
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : t("error.readStatus"));
    }
  }, [projectId, t, username]);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  useEffect(() => {
    if (!status || !["queued", "processing"].includes(status.status)) return;
    const timer = window.setInterval(() => void loadStatus(), 4000);
    return () => window.clearInterval(timer);
  }, [loadStatus, status]);

  const start = async () => {
    setStarting(true);
    setError("");
    try {
      const next = await api.startProject(projectId, username);
      setStatus(next);
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : t("error.start"));
    } finally {
      setStarting(false);
    }
  };

  const ready = status?.status === "ready";
  const failed = status?.status === "failed";
  const canStart = status && !["queued", "processing", "ready"].includes(status.status);

  return (
    <div className="page-container run-page">
      <header className="page-heading split-heading">
        <div>
          <p className="eyebrow">{t("run.eyebrow")}</p>
          <h1>{t("run.title")}</h1>
          <p>{t("run.subtitle")}</p>
        </div>
        <StatusBadge status={status?.status} />
      </header>

      <section className={`panel run-console ${ready ? "is-ready" : ""}`}>
        <div className="project-id-row">
          <span>PROJECT</span>
          <code>{projectId}</code>
        </div>
        <ProgressBar value={status?.progress || 0} label={status?.current_step} />

        <div className="run-details">
          <div>
            <span>{t("run.currentStage")}</span>
            <strong>{status?.message || t("run.connecting")}</strong>
          </div>
          <div>
            <span>{t("run.elapsed")}</span>
            <strong>
              {status?.elapsed_seconds
                ? `${Math.floor(status.elapsed_seconds / 60)}m ${status.elapsed_seconds % 60}s`
                : "—"}
            </strong>
          </div>
          <div>
            <span>{t("run.remaining")}</span>
            <strong>{status?.estimated_remaining_text || "—"}</strong>
          </div>
          <div>
            <span>{t("run.scope")}</span>
            <strong>
              {status?.current_chunk && status.processing_chunk_total
                ? `Chunk ${status.current_chunk}/${status.processing_chunk_total}`
                : status?.current_batch && status.processing_batch_total
                  ? `Batch ${status.current_batch}/${status.processing_batch_total}`
                  : `${status?.selected_chunks || "—"} chunks`}
            </strong>
          </div>
        </div>

        {status?.warnings?.length ? (
          <div className="warning-list">
            {status.warnings.map((warning) => <p key={warning}>{warning}</p>)}
          </div>
        ) : null}
        <ErrorBox message={error || (failed ? status?.error || t("error.buildFailed") : "")} />

        {canStart && (
          <button className="button primary large" onClick={start} disabled={starting}>
            <SparkIcon />
            {starting ? t("run.starting") : t("run.start")}
          </button>
        )}

        {ready && (
          <div className="ready-block">
            <span className="ready-icon"><CheckIcon /></span>
            <div>
              <h2>{t("run.complete")}</h2>
              <p>{t("run.completeText")}</p>
            </div>
            <div className="ready-actions">
              <Link className="button primary" to={`/projects/${projectId}`}>
                {t("run.dashboard")} <ArrowIcon />
              </Link>
              <Link className="button ghost" to={`/projects/${projectId}/chat`}>
                {t("run.simulate")}
              </Link>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
