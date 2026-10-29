import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError } from "../api/client";
import type {
  EstimateResponse,
  LLMProfile,
  SourcePreviewResponse,
} from "../api/types";
import { ErrorBox } from "../components/ErrorBox";
import { ArrowIcon, CheckIcon, UploadIcon } from "../components/Icons";
import { getStoredUsername, rememberProject } from "../lib/storage";
import { useLanguage } from "../i18n";

const MAX_BYTES = 6 * 1024 * 1024;

function fileSize(bytes: number) {
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}

export function NewProjectPage() {
  const { t } = useLanguage();
  const navigate = useNavigate();
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [username, setUsername] = useState(getStoredUsername());
  const [chunkSize, setChunkSize] = useState(3000);
  const [overlap, setOverlap] = useState(300);
  const [selectedChunks, setSelectedChunks] = useState(2);
  const [demoMode, setDemoMode] = useState(true);
  const [estimate, setEstimate] = useState<EstimateResponse | null>(null);
  const [preview, setPreview] = useState<SourcePreviewResponse | null>(null);
  const [llmProfiles, setLlmProfiles] = useState<LLMProfile[]>([]);
  const [modelOptions, setModelOptions] = useState<string[]>([]);
  const [llmProfile, setLlmProfile] = useState<LLMProfile>({
    profile_name: "Local LM Studio",
    llm_base_url: "http://localhost:1234/v1",
    llm_model: "gemma-4-26b-a4b-it",
    llm_api_key: "lm-studio",
  });
  const [llmStatus, setLlmStatus] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState<"llm" | "estimate" | "preview" | "create" | null>(null);

  useEffect(() => {
    api.getLLMProfiles()
      .then((result) => {
        setLlmProfiles(result.profiles || []);
        const active =
          result.profiles?.find(
            (item) => item.profile_name === result.active_llm_profile,
          ) || result.profiles?.[0];
        if (active) setLlmProfile(active);
      })
      .catch(() => {
        setLlmStatus(t("llm.loadFailed"));
      });
  }, [t]);

  const chosenTime = useMemo(() => {
    if (!estimate) return "";
    const seconds =
      selectedChunks * (estimate.seconds_per_chunk || 60) +
      (estimate.estimated_pipeline_overhead_seconds || 0);
    const minutes = Math.ceil(seconds / 60);
    return minutes < 60
      ? t("new.aboutMinutes", { minutes })
      : t("new.aboutHours", {
          hours: Math.floor(minutes / 60),
          minutes: minutes % 60,
        });
  }, [estimate, selectedChunks, t]);

  const acceptFile = (next?: File) => {
    setError("");
    setEstimate(null);
    if (!next) return;
    if (!next.name.toLowerCase().endsWith(".txt")) {
      setError(t("error.txtOnly"));
      return;
    }
    if (next.size > MAX_BYTES) {
      setError(t("error.fileTooLarge"));
      return;
    }
    setFile(next);
  };

  const validate = () => {
    if (!file) return t("error.chooseNovel");
    if (!username.trim()) return t("error.usernameForSave");
    if (chunkSize < 500) return t("error.chunkSize");
    if (overlap < 0 || overlap >= chunkSize)
      return t("error.overlap");
    return "";
  };

  const estimateProject = async () => {
    const validation = validate();
    if (validation) return setError(validation);
    setError("");
    setLoading("estimate");
    try {
      const result = await api.estimateProject(file!, chunkSize, overlap);
      setEstimate(result);
      setPreview(null);
      const total = result.estimated_total_chunks || 1;
      setSelectedChunks((current) =>
        Math.min(total, demoMode ? Math.min(current || 2, 2) : current || total),
      );
    } catch (reason) {
      setError(
        reason instanceof ApiError ? reason.message : t("error.estimate"),
      );
    } finally {
      setLoading(null);
    }
  };

  const previewSource = async () => {
    const validation = validate();
    if (validation) return setError(validation);
    if (!estimate) return setError(t("error.estimateFirst"));
    setError("");
    setLoading("preview");
    try {
      const result = await api.previewSourceMoment(
        file!,
        selectedChunks,
        chunkSize,
        overlap,
      );
      setPreview(result);
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : t("error.preview"));
    } finally {
      setLoading(null);
    }
  };

  const createProject = async () => {
    const validation = validate();
    if (validation) return setError(validation);
    if (!estimate) return setError(t("error.estimateFirst"));
    if (
      selectedChunks < 1 ||
      selectedChunks > (estimate.estimated_total_chunks || 1)
    ) {
      return setError(t("error.chunkRange"));
    }
    setError("");
    setLoading("create");
    try {
      const result = await api.createProject({
        username: username.trim(),
        file: file!,
        selectedChunks,
        chunkSize,
        overlap,
      });
      rememberProject(result.project_id, username);
      navigate(`/projects/${encodeURIComponent(result.project_id)}/run`);
    } catch (reason) {
      setError(
        reason instanceof ApiError ? reason.message : t("error.createProject"),
      );
    } finally {
      setLoading(null);
    }
  };

  const selectLLMProfile = async (profileName: string) => {
    const selected = llmProfiles.find((item) => item.profile_name === profileName);
    if (selected) setLlmProfile(selected);
    setLlmStatus("");
    setLoading("llm");
    try {
      const result = await api.activateLLMProfile(profileName);
      setLlmProfiles(result.profiles || []);
      const active =
        result.profiles?.find(
          (item) => item.profile_name === result.active_llm_profile,
        ) || selected;
      if (active) setLlmProfile(active);
      setLlmStatus(t("llm.activeSaved"));
    } catch (reason) {
      setLlmStatus(reason instanceof ApiError ? reason.message : t("llm.saveFailed"));
    } finally {
      setLoading(null);
    }
  };

  const saveLLMProfile = async () => {
    setLlmStatus("");
    setLoading("llm");
    try {
      const result = await api.saveLLMProfile({ ...llmProfile, make_active: true });
      setLlmProfiles(result.profiles || []);
      const active =
        result.profiles?.find(
          (item) => item.profile_name === result.active_llm_profile,
        ) || llmProfile;
      setLlmProfile(active);
      setLlmStatus(t("llm.saved"));
    } catch (reason) {
      setLlmStatus(reason instanceof ApiError ? reason.message : t("llm.saveFailed"));
    } finally {
      setLoading(null);
    }
  };

  const deleteLLMProfile = async () => {
    if (!llmProfile.profile_name.trim()) return;
    setLlmStatus("");
    setLoading("llm");
    try {
      const result = await api.deleteLLMProfile(llmProfile.profile_name);
      setLlmProfiles(result.profiles || []);
      const active =
        result.profiles?.find(
          (item) => item.profile_name === result.active_llm_profile,
        ) || result.profiles?.[0];
      if (active) setLlmProfile(active);
      setLlmStatus(t("llm.deleted"));
    } catch (reason) {
      setLlmStatus(reason instanceof ApiError ? reason.message : t("llm.deleteFailed"));
    } finally {
      setLoading(null);
    }
  };

  const checkLLMProfile = async () => {
    setLlmStatus("");
    setLoading("llm");
    try {
      await api.saveLLMProfile({ ...llmProfile, make_active: true });
      const result = await api.checkLLMProfile();
      const models = result.models || [];
      setModelOptions(models);
      if (models.length && !models.includes(llmProfile.llm_model)) {
        setLlmProfile({ ...llmProfile, llm_model: models[0] });
      }
      setLlmStatus(
        result.selected_model_found
          ? t("llm.checkOk", { model: result.selected_model || "" })
          : t("llm.checkMissing", { model: result.selected_model || "" }),
      );
    } catch (reason) {
      setLlmStatus(reason instanceof ApiError ? reason.message : t("llm.checkFailed"));
    } finally {
      setLoading(null);
    }
  };

  return (
    <div className="page-container narrow">
      <header className="page-heading">
        <p className="eyebrow">{t("new.eyebrow")}</p>
        <h1>{t("new.title")}</h1>
        <p>{t("new.subtitle")}</p>
      </header>

      <section className="panel llm-panel">
        <div className="section-heading">
          <div>
            <p className="eyebrow">{t("llm.eyebrow")}</p>
            <h2>{t("llm.title")}</h2>
          </div>
          <button
            className="button secondary"
            disabled={loading === "llm"}
            onClick={checkLLMProfile}
          >
            {t("llm.check")}
          </button>
        </div>
        <div className="form-grid">
          <label className="field">
            <span>{t("llm.profile")}</span>
            <select
              value={llmProfile.profile_name}
              onChange={(event) => selectLLMProfile(event.target.value)}
            >
              {llmProfiles.map((profile) => (
                <option key={profile.profile_name} value={profile.profile_name}>
                  {profile.profile_name}
                </option>
              ))}
              {!llmProfiles.some((item) => item.profile_name === llmProfile.profile_name) && (
                <option value={llmProfile.profile_name}>{llmProfile.profile_name}</option>
              )}
            </select>
          </label>
          <label className="field">
            <span>{t("llm.profileName")}</span>
            <input
              value={llmProfile.profile_name}
              onChange={(event) =>
                setLlmProfile({ ...llmProfile, profile_name: event.target.value })
              }
            />
          </label>
          <label className="field span-2">
            <span>{t("llm.baseUrl")}</span>
            <input
              value={llmProfile.llm_base_url}
              onChange={(event) =>
                setLlmProfile({ ...llmProfile, llm_base_url: event.target.value })
              }
            />
          </label>
          <label className="field">
            <span>{t("llm.model")}</span>
            {modelOptions.length ? (
              <select
                value={llmProfile.llm_model}
                onChange={(event) =>
                  setLlmProfile({ ...llmProfile, llm_model: event.target.value })
                }
              >
                {modelOptions.map((model) => (
                  <option key={model} value={model}>{model}</option>
                ))}
              </select>
            ) : (
              <input
                value={llmProfile.llm_model}
                onChange={(event) =>
                  setLlmProfile({ ...llmProfile, llm_model: event.target.value })
                }
              />
            )}
          </label>
          <label className="field">
            <span>{t("llm.apiKey")}</span>
            <input
              type="password"
              value={llmProfile.llm_api_key}
              onChange={(event) =>
                setLlmProfile({ ...llmProfile, llm_api_key: event.target.value })
              }
            />
          </label>
        </div>
        {llmStatus && <p className="llm-status">{llmStatus}</p>}
        <div className="button-row">
          <button
            className="button secondary"
            disabled={loading === "llm"}
            onClick={saveLLMProfile}
          >
            {t("llm.save")}
          </button>
          <button
            className="button ghost"
            disabled={loading === "llm"}
            onClick={deleteLLMProfile}
          >
            {t("llm.delete")}
          </button>
        </div>
      </section>

      <section className="panel upload-panel">
        <div
          className={`drop-zone ${file ? "has-file" : ""}`}
          onClick={() => inputRef.current?.click()}
          onDragOver={(event) => event.preventDefault()}
          onDrop={(event) => {
            event.preventDefault();
            acceptFile(event.dataTransfer.files[0]);
          }}
        >
          <input
            ref={inputRef}
            type="file"
            accept=".txt,text/plain"
            hidden
            onChange={(event) => acceptFile(event.target.files?.[0])}
          />
          <span className="drop-icon">
            {file ? <CheckIcon /> : <UploadIcon />}
          </span>
          {file ? (
            <>
              <strong>{file.name}</strong>
              <small>{fileSize(file.size)} · {t("new.changeFile")}</small>
            </>
          ) : (
            <>
              <strong>{t("new.dropFile")}</strong>
              <small>{t("new.fileLimit")}</small>
            </>
          )}
        </div>

        <div className="form-grid">
          <label className="field span-2">
            <span>{t("common.username")}</span>
            <input
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              placeholder={t("new.usernamePlaceholder")}
            />
            <small>{t("new.usernameHelp")}</small>
          </label>
          <label className="field">
            <span>Chunk size</span>
            <input
              type="number"
              value={chunkSize}
              min={500}
              onChange={(event) => {
                setChunkSize(Number(event.target.value));
                setEstimate(null);
              }}
            />
          </label>
          <label className="field">
            <span>Overlap</span>
            <input
              type="number"
              value={overlap}
              min={0}
              onChange={(event) => {
                setOverlap(Number(event.target.value));
                setEstimate(null);
              }}
            />
          </label>
          <label className="toggle-row span-2">
            <input
              type="checkbox"
              checked={demoMode}
              onChange={(event) => {
                setDemoMode(event.target.checked);
                if (event.target.checked) setSelectedChunks(2);
              }}
            />
            <span className="toggle" />
            <span>
              <strong>{t("new.demo")}</strong>
              <small>{t("new.demoHelp")}</small>
            </span>
          </label>
        </div>

        <ErrorBox message={error} />
        <button
          className="button secondary full"
          disabled={loading !== null}
          onClick={estimateProject}
        >
          {loading === "estimate" ? t("new.estimating") : t("new.estimate")}
        </button>
      </section>

      {estimate && (
        <section className="panel estimate-panel">
          <div className="section-heading">
            <div>
              <p className="eyebrow">ESTIMATE</p>
              <h2>{t("new.scale")}</h2>
            </div>
            <span className="estimate-time">{estimate.estimated_full_text}</span>
          </div>
          <div className="metric-grid">
            <div><span>{t("new.characters")}</span><strong>{estimate.character_count?.toLocaleString() || "—"}</strong></div>
            <div><span>{t("new.sections")}</span><strong>{estimate.section_count?.toLocaleString() || "—"}</strong></div>
            <div><span>{t("new.totalChunks")}</span><strong>{estimate.estimated_total_chunks?.toLocaleString() || "—"}</strong></div>
            <div><span>{t("new.perChunk")}</span><strong>{estimate.seconds_per_chunk || 60}s</strong></div>
          </div>
          <label className="range-field">
            <span>
              <strong>{t("new.processChunks", { count: selectedChunks })}</strong>
              <em>{chosenTime}</em>
            </span>
            <input
              type="range"
              min={1}
              max={Math.max(1, estimate.estimated_total_chunks || 1)}
              value={selectedChunks}
              onChange={(event) => {
                setSelectedChunks(Number(event.target.value));
                setDemoMode(false);
              }}
            />
            <span className="range-labels">
              <small>1</small>
              <small>{estimate.estimated_total_chunks || 1}</small>
            </span>
          </label>
          <label className="field">
            <span>{t("new.exactChunks")}</span>
            <input
              type="number"
              min={1}
              max={estimate.estimated_total_chunks || 1}
              value={selectedChunks}
              onChange={(event) => {
                setSelectedChunks(Number(event.target.value));
                setDemoMode(false);
              }}
            />
          </label>
          <p className="estimate-note">{estimate.note}</p>
          <button
            className="button secondary full"
            disabled={loading !== null}
            onClick={previewSource}
          >
            {loading === "preview" ? t("new.previewing") : t("new.preview")}
          </button>
          {preview && (
            <div className="preview-panel">
              <h3>{t("new.previewTitle")}</h3>
              <p>{preview.summary}</p>
              <details>
                <summary>{t("new.previewExcerpt")}</summary>
                <pre>{preview.excerpt}</pre>
              </details>
            </div>
          )}
          <button
            className="button primary full large"
            disabled={loading !== null}
            onClick={createProject}
          >
            {loading === "create" ? t("new.uploading") : t("new.create")}
            <ArrowIcon />
          </button>
        </section>
      )}
    </div>
  );
}
