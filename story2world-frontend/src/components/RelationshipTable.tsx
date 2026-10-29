import type { Relationship } from "../api/types";
import { useLanguage } from "../i18n";

export function RelationshipTable({
  relationships,
}: {
  relationships: Relationship[];
}) {
  const { t } = useLanguage();
  if (!relationships.length) {
    return <div className="empty-state">{t("relationship.empty")}</div>;
  }
  return (
    <div className="table-scroll">
      <table className="relationship-table">
        <thead>
          <tr>
            <th>{t("relationship.character")}</th>
            <th>{t("relationship.relation")}</th>
            <th>{t("relationship.target")}</th>
            <th>{t("relationship.evidence")}</th>
            <th>{t("relationship.confidence")}</th>
          </tr>
        </thead>
        <tbody>
          {relationships.map((item, index) => (
            <tr key={`${item.canonical_source_id}-${item.canonical_target_id}-${index}`}>
              <td>{item.source_character || "—"}</td>
              <td>
                <span className="relation-pill">
                  {item.relationship_type || t("relationship.unclassified")}
                </span>
              </td>
              <td>{item.target_character || "—"}</td>
              <td>
                <strong>{item.description || "—"}</strong>
                {item.source_text && <small>“{item.source_text}”</small>}
              </td>
              <td>{item.confidence ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
