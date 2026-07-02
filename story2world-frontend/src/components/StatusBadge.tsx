import { useLanguage } from "../i18n";

export function StatusBadge({ status }: { status?: string }) {
  const { t } = useLanguage();
  const value = status || "unknown";
  const labels: Record<string, string> = {
    uploaded: t("status.uploaded"),
    queued: t("status.queued"),
    processing: t("status.processing"),
    ready: t("status.ready"),
    failed: t("status.failed"),
  };
  return (
    <span className={`status-badge status-${value}`}>
      <i />
      {labels[value] || value}
    </span>
  );
}
