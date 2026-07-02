import type { Character } from "../api/types";
import { useLanguage } from "../i18n";
import { ChatIcon } from "./Icons";

export function CharacterList({
  characters,
  selectedId,
  onSelect,
}: {
  characters: Character[];
  selectedId?: string;
  onSelect?: (character: Character) => void;
}) {
  const { t } = useLanguage();
  return (
    <div className="character-list">
      {characters.map((character) => (
        <button
          key={character.character_id}
          className={`character-card ${
            selectedId === character.character_id ? "selected" : ""
          }`}
          onClick={() => onSelect?.(character)}
          type="button"
        >
          <span className="avatar">
            {(character.name || "?").slice(0, 1)}
          </span>
          <span className="character-copy">
            <span className="character-name">
              {character.name || t("character.unnamed")}
              {character.available_as_agent && <ChatIcon />}
            </span>
            <small>
              {[character.tier, ...(character.titles || [])]
                .filter(Boolean)
                .join(" · ") || t("character.reference")}
            </small>
          </span>
          <span className="character-stats">
            {t("character.relationshipCount", {
              count: character.relationship_count || 0,
            })}
          </span>
        </button>
      ))}
    </div>
  );
}
