import type {
  CharactersResponse,
  ChatProgress,
  ChatResponse,
  ChatSaveResponse,
  ChatSessionResponse,
  CreateProjectResponse,
  Dashboard,
  EstimateResponse,
  LLMCheckResponse,
  LLMProfile,
  LLMProfilesResponse,
  AnchorPreviewResponse,
  ProjectStatus,
  RelationshipsResponse,
  SourcePreviewResponse,
  TokenUsageResponse,
  UserProjectsResponse,
  WorldData,
} from "./types";
import { getCurrentLanguage, translateCurrent } from "../i18n";

const rawBase = import.meta.env.VITE_API_BASE?.trim();
export const API_BASE = (rawBase || "").replace(/\/+$/, "");

export class ApiError extends Error {
  status?: number;
  code: "unauthorized" | "offline" | "http";

  constructor(
    message: string,
    code: ApiError["code"],
    status?: number,
  ) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

function queryString(params: Record<string, string | number | undefined>) {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== "") query.set(key, String(value));
  });
  const text = query.toString();
  return text ? `?${text}` : "";
}

function safeMessage(payload: unknown, fallback: string) {
  if (
    payload &&
    typeof payload === "object" &&
    "detail" in payload &&
    typeof payload.detail === "string"
  ) {
    const detail = payload.detail.split("\n")[0].slice(0, 240);
    if (getCurrentLanguage() === "zh" || !/[\u3400-\u9fff]/.test(detail)) {
      return detail;
    }
  }
  return fallback;
}

export async function baseFetch<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  if (!API_BASE) {
    throw new ApiError(
      translateCurrent("api.notConfigured"),
      "offline",
    );
  }
  const headers = new Headers(init.headers);
  if (!(init.body instanceof FormData) && init.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, { ...init, headers });
  } catch {
    throw new ApiError(translateCurrent("api.offline"), "offline");
  }

  let payload: unknown = null;
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    payload = await response.json().catch(() => null);
  }

  if (response.status === 401) {
    throw new ApiError(translateCurrent("api.unauthorized"), "unauthorized", 401);
  }
  if (!response.ok) {
    if (response.status >= 500) {
      throw new ApiError(
        translateCurrent("api.failed"),
        "http",
        response.status,
      );
    }
    throw new ApiError(
      safeMessage(
        payload,
        translateCurrent("api.requestFailed", { status: response.status }),
      ),
      "http",
      response.status,
    );
  }
  return payload as T;
}

async function streamFetch<T>(
  path: string,
  init: RequestInit,
  onProgress: (progress: ChatProgress) => void,
): Promise<T> {
  if (!API_BASE) {
    throw new ApiError(translateCurrent("api.notConfigured"), "offline");
  }
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...(init.headers || {}),
      },
    });
  } catch {
    throw new ApiError(translateCurrent("api.offline"), "offline");
  }
  if (!response.ok || !response.body) {
    const payload = await response.json().catch(() => null);
    throw new ApiError(
      safeMessage(
        payload,
        translateCurrent("api.requestFailed", { status: response.status }),
      ),
      "http",
      response.status,
    );
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result: T | null = null;
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) {
      if (!line.trim()) continue;
      const event = JSON.parse(line) as {
        type: string;
        data?: T | ChatProgress;
        error?: string;
      };
      if (event.type === "progress") {
        onProgress(event.data as ChatProgress);
      } else if (event.type === "result") {
        result = event.data as T;
      } else if (event.type === "error") {
        throw new ApiError(
          (
            getCurrentLanguage() === "zh" && event.error
              ? event.error
              : translateCurrent("api.chatFailed")
          ).slice(0, 240),
          "http",
          500,
        );
      }
    }
    if (done) break;
  }
  if (!result) {
    throw new ApiError(translateCurrent("api.emptyResult"), "http", 500);
  }
  return result;
}

export const api = {
  health: () => baseFetch<{ status: string }>("/health"),

  getLLMProfiles() {
    return baseFetch<LLMProfilesResponse>("/llm/profiles");
  },

  saveLLMProfile(profile: LLMProfile & { make_active?: boolean }) {
    return baseFetch<LLMProfilesResponse>("/llm/profiles", {
      method: "POST",
      body: JSON.stringify({
        ...profile,
        make_active: profile.make_active ?? true,
      }),
    });
  },

  activateLLMProfile(profileName: string) {
    return baseFetch<LLMProfilesResponse>("/llm/profiles/active", {
      method: "POST",
      body: JSON.stringify({ profile_name: profileName }),
    });
  },

  deleteLLMProfile(profileName: string) {
    return baseFetch<LLMProfilesResponse>(
      `/llm/profiles/${encodeURIComponent(profileName)}`,
      { method: "DELETE" },
    );
  },

  checkLLMProfile() {
    return baseFetch<LLMCheckResponse>("/llm/check", { method: "POST" });
  },

  getTokenUsage() {
    return baseFetch<TokenUsageResponse>("/llm/usage");
  },

  estimateProject(file: File, chunkSize: number, overlap: number) {
    const body = new FormData();
    body.append("file", file);
    body.append("chunk_size", String(chunkSize));
    body.append("overlap", String(overlap));
    return baseFetch<EstimateResponse>("/projects/estimate", {
      method: "POST",
      body,
    });
  },

  previewSourceMoment(
    file: File,
    selectedChunks: number,
    chunkSize: number,
    overlap: number,
  ) {
    const body = new FormData();
    body.append("file", file);
    body.append("selected_chunks", String(selectedChunks));
    body.append("chunk_size", String(chunkSize));
    body.append("overlap", String(overlap));
    return baseFetch<SourcePreviewResponse>("/projects/source-preview", {
      method: "POST",
      body,
    });
  },

  createProject(input: {
    username: string;
    file: File;
    selectedChunks: number;
    chunkSize: number;
    overlap: number;
  }) {
    const body = new FormData();
    body.append("username", input.username);
    body.append("file", input.file);
    body.append("selected_chunks", String(input.selectedChunks));
    body.append("chunk_size", String(input.chunkSize));
    body.append("overlap", String(input.overlap));
    body.append("auto_start", "false");
    return baseFetch<CreateProjectResponse>("/projects", {
      method: "POST",
      body,
    });
  },

  startProject(projectId: string, username: string) {
    return baseFetch<ProjectStatus>(
      `/projects/${encodeURIComponent(projectId)}/start${queryString({ username })}`,
      { method: "POST" },
    );
  },

  getStatus(projectId: string, username: string) {
    return baseFetch<ProjectStatus>(
      `/projects/${encodeURIComponent(projectId)}/status${queryString({ username })}`,
    );
  },

  getDashboard(projectId: string, username: string) {
    return baseFetch<Dashboard>(
      `/projects/${encodeURIComponent(projectId)}/dashboard${queryString({ username })}`,
    );
  },

  getCharacters(projectId: string, username: string) {
    return baseFetch<CharactersResponse>(
      `/projects/${encodeURIComponent(projectId)}/characters${queryString({ username })}`,
    );
  },

  getRelationships(projectId: string, username: string) {
    return baseFetch<RelationshipsResponse>(
      `/projects/${encodeURIComponent(projectId)}/relationships${queryString({ username })}`,
    );
  },

  getWorld(projectId: string, username: string) {
    return baseFetch<WorldData>(
      `/projects/${encodeURIComponent(projectId)}/world${queryString({ username })}`,
    );
  },

  chat(
    projectId: string,
    input: {
      username: string;
      sessionId: string;
      characterId: string;
      message: string;
    },
  ) {
    return baseFetch<ChatResponse>(
      `/projects/${encodeURIComponent(projectId)}/chat`,
      {
        method: "POST",
        body: JSON.stringify({
          username: input.username,
          session_id: input.sessionId,
          character_id: input.characterId,
          message: input.message,
        }),
      },
    );
  },

  chatStream(
    projectId: string,
    input: {
      username: string;
      sessionId: string;
      characterId: string;
      message: string;
    },
    onProgress: (progress: ChatProgress) => void,
  ) {
    return streamFetch<ChatResponse>(
      `/projects/${encodeURIComponent(projectId)}/chat/stream`,
      {
        method: "POST",
        body: JSON.stringify({
          username: input.username,
          session_id: input.sessionId,
          character_id: input.characterId,
          message: input.message,
        }),
      },
      onProgress,
    );
  },

  getChatSession(
    projectId: string,
    input: {
      username: string;
      sessionId: string;
      characterId: string;
    },
  ) {
    return baseFetch<ChatSessionResponse>(
      `/projects/${encodeURIComponent(projectId)}/chat/session${queryString({
        username: input.username,
        session_id: input.sessionId,
        character_id: input.characterId,
      })}`,
    );
  },

  getAnchorPreview(
    projectId: string,
    input: {
      username: string;
      sessionId: string;
      characterId: string;
      progressPercent?: number;
    },
  ) {
    return baseFetch<AnchorPreviewResponse>(
      `/projects/${encodeURIComponent(projectId)}/chat/anchor-preview${queryString({
        username: input.username,
        session_id: input.sessionId,
        character_id: input.characterId,
        progress_percent: input.progressPercent,
      })}`,
    );
  },

  saveChatSession(
    projectId: string,
    input: {
      username: string;
      sessionId: string;
      characterId: string;
    },
  ) {
    return baseFetch<ChatSaveResponse>(
      `/projects/${encodeURIComponent(projectId)}/chat/save`,
      {
        method: "POST",
        body: JSON.stringify({
          username: input.username,
          session_id: input.sessionId,
          character_id: input.characterId,
          message: "",
        }),
      },
    );
  },

  getUserProjects(username: string) {
    return baseFetch<UserProjectsResponse>(
      `/users/${encodeURIComponent(username)}/projects`,
    );
  },
};
