const USERNAME_KEY = "story2world.username";
const PROJECT_PREFIX = "story2world.project.";

export function getStoredUsername() {
  return localStorage.getItem(USERNAME_KEY) || "";
}

export function setStoredUsername(username: string) {
  localStorage.setItem(USERNAME_KEY, username.trim());
}

export function rememberProject(projectId: string, username: string) {
  localStorage.setItem(`${PROJECT_PREFIX}${projectId}`, username.trim());
  setStoredUsername(username);
}

export function getProjectUsername(projectId: string) {
  return (
    localStorage.getItem(`${PROJECT_PREFIX}${projectId}`) ||
    getStoredUsername()
  );
}

export function getSessionId(projectId: string, characterId: string) {
  const key = `story2world.session.${projectId}.${characterId}`;
  const existing = localStorage.getItem(key);
  if (existing) return existing;
  const value = `web_${Date.now().toString(36)}_${Math.random()
    .toString(36)
    .slice(2, 8)}`;
  localStorage.setItem(key, value);
  return value;
}
