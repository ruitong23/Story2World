import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type { TokenUsageResponse } from "../api/types";
import { ErrorBox } from "../components/ErrorBox";
import { useLanguage } from "../i18n";

function fmt(value?: number) {
  return (value || 0).toLocaleString();
}

export function TokenUsagePage() {
  const { t } = useLanguage();
  const [usage, setUsage] = useState<TokenUsageResponse | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = async () => {
    setLoading(true);
    setError("");
    try {
      setUsage(await api.getTokenUsage());
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : t("tokens.loadFailed"));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  return (
    <div className="page-container">
      <header className="page-heading">
        <p className="eyebrow">{t("tokens.eyebrow")}</p>
        <h1>{t("tokens.title")}</h1>
        <p>{t("tokens.subtitle")}</p>
      </header>
      <ErrorBox message={error} />
      <section className="panel">
        <div className="section-heading">
          <div>
            <p className="eyebrow">TOTAL</p>
            <h2>{loading ? t("common.loading") : fmt(usage?.totals.total_tokens)}</h2>
          </div>
          <button className="button secondary" onClick={load} disabled={loading}>
            {t("tokens.refresh")}
          </button>
        </div>
        <div className="metric-grid">
          <div><span>{t("tokens.calls")}</span><strong>{fmt(usage?.totals.call_count)}</strong></div>
          <div><span>{t("tokens.prompt")}</span><strong>{fmt(usage?.totals.prompt_tokens)}</strong></div>
          <div><span>{t("tokens.completion")}</span><strong>{fmt(usage?.totals.completion_tokens)}</strong></div>
          <div><span>{t("tokens.total")}</span><strong>{fmt(usage?.totals.total_tokens)}</strong></div>
        </div>
      </section>

      <section className="dashboard-grid token-grid">
        <div className="panel">
          <div className="section-heading"><h2>{t("tokens.bySource")}</h2></div>
          <div className="token-table">
            {(usage?.by_source || []).map((item) => (
              <div key={item.name}>
                <strong>{item.name}</strong>
                <span>{fmt(item.total_tokens)} / {fmt(item.call_count)} calls</span>
              </div>
            ))}
          </div>
        </div>
        <div className="panel">
          <div className="section-heading"><h2>{t("tokens.byModel")}</h2></div>
          <div className="token-table">
            {(usage?.by_model || []).map((item) => (
              <div key={item.name}>
                <strong>{item.name}</strong>
                <span>{fmt(item.total_tokens)} / {fmt(item.call_count)} calls</span>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="panel">
        <div className="section-heading"><h2>{t("tokens.recent")}</h2></div>
        <div className="token-table">
          {(usage?.recent || []).map((item, index) => (
            <div key={`${item.timestamp}-${index}`}>
              <strong>{item.source} {"->"} {item.flow}</strong>
              <span>
                {item.model} | {fmt(item.total_tokens)} tokens
                {item.estimated ? " | estimated" : ""}
              </span>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
