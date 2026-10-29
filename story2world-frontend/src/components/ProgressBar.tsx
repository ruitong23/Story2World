import { useLanguage } from "../i18n";

export function ProgressBar({
  value = 0,
  label,
}: {
  value?: number;
  label?: string;
}) {
  const { t } = useLanguage();
  const normalized = Math.max(0, Math.min(1, value));
  return (
    <div className="progress-wrap">
      <div className="progress-meta">
        <span>{label || t("progress.default")}</span>
        <strong>{Math.round(normalized * 100)}%</strong>
      </div>
      <div
        className="progress-track"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(normalized * 100)}
      >
        <span style={{ width: `${normalized * 100}%` }} />
      </div>
    </div>
  );
}
