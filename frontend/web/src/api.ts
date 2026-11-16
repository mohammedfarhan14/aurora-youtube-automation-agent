export const API_BASE_URL = (
  import.meta.env.VITE_API_BASE_URL ?? "/api/v1"
).replace(/\/$/, "");

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export type User = {
  id: string;
  email: string;
  display_name: string;
  is_active: boolean;
  email_verified: boolean;
};

export type Organization = {
  id: string;
  name: string;
  slug: string;
  plan: string;
  status: string;
  created_at: string;
  updated_at: string;
};

export type OrganizationStudioSettings = {
  research_enabled: boolean;
  video_generation_enabled: boolean;
  automatic_publishing_enabled: boolean;
  target_duration_seconds: 15 | 30 | 45;
  qwen_thinking_mode: "off" | "on";
  story_max_output_tokens: number | null;
  story_timeout_seconds: number;
  language: string;
  tone: string;
  audience: string;
  additional_rules: string[];
};

export type OrganizationStudioSettingsResponse = {
  settings: OrganizationStudioSettings;
  updated_at: string;
  updated_by_user_id: string | null;
  can_edit: boolean;
};

export type ProductionReadinessCheck = {
  key: string;
  status: "ready" | "attention" | "blocked";
  title: string;
  detail: string;
  action: string | null;
};

export type ProductionReadiness = {
  summary: "ready" | "attention" | "blocked";
  checks: ProductionReadinessCheck[];
  tts_provider: string | null;
  tts_voice_options: string[];
};

export type YouTubeChannel = {
  id: string;
  channel_id: string;
  title: string;
  custom_url: string | null;
  thumbnail_url: string | null;
  granted_scopes: string[];
  can_publish: boolean;
  connected_at: string;
};

export type YouTubeAnalyticsDay = {
  day: string;
  views: number;
  minutes_watched: number;
  average_view_duration_seconds: number;
  average_view_percentage: number;
  subscribers_gained: number;
  subscribers_lost: number;
  likes: number;
  comments: number;
  shares: number;
};

export type YouTubeAnalyticsComparison = {
  current_start_date: string;
  current_end_date: string;
  previous_start_date: string;
  previous_end_date: string;
  current_views: number;
  previous_views: number;
  current_watch_minutes: number;
  previous_watch_minutes: number;
  current_average_view_percentage: number;
  previous_average_view_percentage: number;
  views_change_percent: number | null;
  watch_minutes_change_percent: number | null;
  average_view_percentage_change_points: number;
  suggestion: string;
  review: YouTubeAnalyticsReview;
};

export type YouTubeAnalyticsReview = {
  id: string;
  status: "pending" | "approved" | "dismissed";
  reviewed_at: string | null;
  reviewed_by_user_id: string | null;
};

export type YouTubeAnalytics = {
  youtube_channel_id: string;
  channel_title: string;
  start_date: string;
  end_date: string;
  latest_report_day: string | null;
  pending_days: number;
  verified: true;
  snapshot_count: number;
  snapshot_saved_at: string | null;
  comparison: YouTubeAnalyticsComparison | null;
  rows: YouTubeAnalyticsDay[];
};

type ApiOptions = Omit<RequestInit, "headers"> & {
  headers?: HeadersInit;
};

let csrfToken: string | null = null;
let csrfRequest: Promise<string> | null = null;

async function fetchApi(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const method = (init?.method ?? "GET").toUpperCase();
  const retryDelays = method === "GET" || method === "HEAD" ? [0, 300, 900, 1_800, 3_000] : [0];
  let lastNetworkError: unknown;

  for (const delay of retryDelays) {
    if (delay) await new Promise((resolve) => setTimeout(resolve, delay));
    try {
      return await fetch(input, init);
    } catch (error) {
      if (!(error instanceof TypeError)) throw error;
      lastNetworkError = error;
    }
  }

  throw new Error(
    "Aurora API could not be reached. Check that the backend is running on port 8000 and that the /api proxy is available, then retry.",
    { cause: lastNetworkError },
  );
}

export function setCsrfToken(token: string): void {
  csrfToken = token;
}

export function clearCsrfToken(): void {
  csrfToken = null;
}

export function clearLegacyAuthToken(): void {
  try {
    window.sessionStorage.removeItem("aurora_access_token");
  } catch {
    // Storage may be disabled; the client never reads or writes auth tokens.
  }
}

export function apiUrl(path: string): string {
  return `${API_BASE_URL}${path.startsWith("/") ? path : `/${path}`}`;
}

async function ensureCsrfToken(): Promise<string> {
  if (csrfToken) return csrfToken;
  if (csrfRequest) return csrfRequest;

  csrfRequest = (async () => {
    const response = await fetchApi(apiUrl("/auth/csrf"), {
      method: "GET",
      credentials: "same-origin",
      cache: "no-store",
    });
    const payload: unknown = await response.json().catch(() => null);
    if (!response.ok) {
      throw new ApiError(
        extractApiError(payload, response.statusText),
        response.status,
      );
    }
    if (
      typeof payload !== "object" ||
      payload === null ||
      !("csrf_token" in payload) ||
      typeof (payload as { csrf_token?: unknown }).csrf_token !== "string"
    ) {
      throw new ApiError("Unable to initialize request security.", response.status);
    }
    csrfToken = (payload as { csrf_token: string }).csrf_token;
    return csrfToken;
  })();

  try {
    return await csrfRequest;
  } finally {
    csrfRequest = null;
  }
}

export async function apiRequest<T>(
  path: string,
  options: ApiOptions = {},
): Promise<T> {
  const { headers, ...requestOptions } = options;
  const requestHeaders = new Headers(headers);
  const method = (requestOptions.method ?? "GET").toUpperCase();

  if (requestOptions.body && !requestHeaders.has("Content-Type")) {
    requestHeaders.set("Content-Type", "application/json");
  }
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    requestHeaders.set("X-CSRF-Token", await ensureCsrfToken());
  }

  const url = apiUrl(path);
  const init: RequestInit = {
    ...requestOptions,
    credentials: requestOptions.credentials ?? "same-origin",
    headers: requestHeaders,
  };
  let response = await fetchApi(url, init);

  if (response.status === 403 && !["GET", "HEAD", "OPTIONS"].includes(method)) {
    const failure: unknown = await response.clone().json().catch(() => null);
    if (extractApiError(failure, "") === "CSRF validation failed.") {
      clearCsrfToken();
      requestHeaders.set("X-CSRF-Token", await ensureCsrfToken());
      response = await fetchApi(url, { ...init, headers: requestHeaders });
    }
  }

  if (response.status === 204) return undefined as T;

  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ApiError(
      extractApiError(payload, response.statusText),
      response.status,
    );
  }
  return payload as T;
}

export function extractApiError(payload: unknown, fallback: string): string {
  if (
    typeof payload === "object" &&
    payload !== null &&
    "detail" in payload
  ) {
    const detail = (payload as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((item) =>
          typeof item === "object" && item !== null && "msg" in item
            ? String((item as { msg: unknown }).msg)
            : String(item),
        )
        .join(", ");
    }
  }
  return fallback || "Something went wrong. Please try again.";
}

/**
 * Revoke the current browser session.
 *
 * A 401 means the server has already invalidated or no longer recognizes the
 * session, so callers may safely clear local authenticated state. Other
 * failures must propagate because the server-side session may still be valid.
 */
export async function logoutBrowserSession(): Promise<void> {
  try {
    await apiRequest<void>("/auth/browser-session/logout", { method: "POST" });
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return;
    throw error;
  }
}
export type ResearchRun = {
  id: string;
  organization_id: string;
  requested_by_user_id: string;
  topic: string;
  status: "queued" | "researching" | "completed" | "failed";
  failure_code: string | null;
  failure_message: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
};

export type RightsStatus =
  | "unknown"
  | "research_only"
  | "review_required"
  | "cleared"
  | "restricted";

export type RightsScope =
  | "unknown"
  | "source_text"
  | "abstract"
  | "metadata"
  | "media_asset";

export type ReuseRestriction =
  | "attribution"
  | "share_alike"
  | "non_commercial"
  | "no_derivatives"
  | "indicate_modifications"
  | "no_verbatim_reuse_without_license"
  | "review_page_history"
  | "unknown";

export type RightsMetadata = {
  status: RightsStatus;
  scope: RightsScope;
  license_ids: string[];
  license_urls: string[];
  rights_holder: string | null;
  creators: string[];
  source_revision_url: string | null;
  attribution_text: string | null;
  reuse_restrictions: ReuseRestriction[];
};

export type ResearchSource = {
  id: string;
  research_run_id: string;
  canonical_url: string;
  title: string;
  publisher: string | null;
  source_type: string;
  published_at: string | null;
  retrieved_at: string;
  content_hash: string | null;
  publisher_domain?: string | null;
  source_class?: string;
  independence_group_key?: string | null;
  independence_basis?: string;
  classification_version?: string;
  rights_metadata: RightsMetadata;
  created_at: string;
};

export type ResearchSourceRightsReviewRequest = {
  decision: "cleared" | "restricted";
  scope: RightsScope;
  license_ids: string[];
  license_urls: string[];
  rights_holder: string | null;
  creators: string[];
  source_revision_url: string | null;
  attribution_text: string | null;
  reuse_restrictions: ReuseRestriction[];
  review_notes: string;
  evidence_urls: string[];
};

export type ResearchSourceRightsReview = {
  id: string;
  organization_id: string;
  research_run_id: string;
  research_source_id: string;
  rights_profile_id: string;
  reviewer_user_id: string;
  decision: "cleared" | "restricted";
  review_notes: string;
  evidence_urls: string[];
  previous_rights_metadata: RightsMetadata;
  resulting_rights_metadata: RightsMetadata;
  created_at: string;
};

export type ResearchSourceRightsReviewResult = {
  review: ResearchSourceRightsReview;
  source: ResearchSource;
  requires_new_story_run: boolean;
};

export type EvidenceItem = {
  id: string;
  research_source_id: string;
  excerpt: string;
  content_hash: string;
  locator: string | null;
};


export type YouTubeChannelChoice = {
  channel_id: string;
  title: string;
  custom_url: string | null;
  thumbnail_url: string | null;
};

export type YouTubePendingSelection = {
  attempt_id: string;
  organization_id: string;
  expires_at: string;
  channels: YouTubeChannelChoice[];
};


export type YouTubeChannelEditorialProfile = {
  id: string;
  organization_id: string;
  youtube_channel_id: string;
  version: number;
  profile: {
    niche: string;
    topic_boundaries: string[];
    target_audience: string;
    language: string;
    tone: string;
    format: "educational" | "documentary" | "news" | "storytelling" | "commentary" | "entertainment";
    prohibited_topics: string[];
    additional_constraints: string[];
    tts_provider?: "openai_compatible" | "kokoro" | "qwen3_tts" | "openai" | "custom" | "elevenlabs" | null;
    tts_voice?: string | null;
    tts_speed?: number | null;
    tts_audio_format?: "wav" | null;
    visual_style?: string;
    visual_lighting?: "natural" | "cinematic" | "studio" | "dramatic";
    visual_motion?: "static" | "gentle_drift";
    video_aspect_ratio?: "9:16" | "16:9" | "1:1";
    caption_style?: "classic" | "bold_outline";
    caption_position?: "lower_safe" | "center";
    caption_size?: "small" | "medium" | "large";
  };
  created_by_user_id: string;
  created_at: string;
};

export type YouTubePublishingSettings = {
  organization_id: string;
  youtube_channel_id: string;
  connected_by_user_id: string | null;
  can_publish: boolean;
  can_manage_auto_publish: boolean;
  auto_publish_enabled: boolean;
  auto_publish_privacy_status: "private" | "unlisted" | "public";
  automatic_execution_available: boolean;
  consent_granted_by_user_id: string | null;
  consent_granted_at: string | null;
};

export type YouTubeChannelSchedule = {
  id: string;
  organization_id: string;
  youtube_channel_id: string;
  enabled: boolean;
  timezone: string;
  posting_time: string;
  catch_up_policy: "skip" | "run_once";
  next_run_at: string | null;
  last_scheduled_local_date: string | null;
  updated_by_user_id: string;
  created_at: string;
  updated_at: string;
};


export type ProductionStage = {
  id: string;
  stage_key: string;
  status: "pending" | "running" | "completed" | "failed" | "skipped" | "cancelled";
  attempt_count: number;
  failure_code: string | null;
  output_manifest: Record<string, unknown>;
  provider_id: string | null;
  model_id: string | null;
  prompt_version: string | null;
  request_id: string | null;
  next_attempt_at: string;
  started_at: string | null;
  completed_at: string | null;
};

export type MediaArtifact = {
  id: string;
  production_stage_id: string;
  artifact_kind: "audio" | "image" | "video" | "captions" | "thumbnail";
  mime_type: string;
  sha256: string;
  byte_size: number;
  duration_ms: number | null;
  width: number | null;
  height: number | null;
  metadata: Record<string, unknown>;
  created_at: string;
  content_url: string;
};

export type ProductionRun = {
  id: string;
  organization_id: string;
  research_run_id: string;
  requested_by_user_id: string;
  youtube_channel_id: string | null;
  channel_profile_version: number | null;
  scheduled_local_date: string | null;
  scheduled_for: string | null;
  status: "queued" | "planning" | "generating" | "verifying" | "awaiting_review" | "approved" | "rejected" | "failed" | "cancelled";
  approved_by_user_id: string | null;
  approved_at: string | null;
  rejected_by_user_id: string | null;
  rejected_at: string | null;
  rejection_reason: string | null;
  approval_mode: "none" | "human" | "automatic_consent";
  approval_consent_event_id: string | null;
  created_at: string;
  updated_at: string;
  stages: ProductionStage[];
};

export type StoryClaim = {
  claim: string;
  verification_status: "supported" | "contradicted" | "conflicting" | "insufficient_evidence" | "unverifiable";
  source_ids: string[];
  evidence: {
    source_id: string;
    evidence_id: string;
    relation: "supports" | "contradicts";
  }[];
  model_confidence: number | null;
};

export type StoryDocument = {
  schema_version: "1.0.0";
  title: string;
  hook: string;
  beats: { heading: string; summary: string }[];
  narration: string;
  visual_direction: string;
  scenes: {
    visual_direction: string;
    narration_segment: string;
    duration_seconds: number;
  }[];
  target_duration_seconds: number;
  word_budget: number;
  source_references: { source_id: string; evidence_ids: string[] }[];
  factual_claims: StoryClaim[];
};

export type StoryArtifact = {
  id: string;
  production_run_id: string;
  artifact_type: "story_draft" | "story_verified";
  schema_version: string;
  version: number;
  content_hash: string;
  created_at: string;
  payload: StoryDocument;
  evidence_context: {
    id: string;
    canonical_url: string;
    title: string;
    publisher: string | null;
    source_type: string;
    evidence: { id: string; excerpt: string; locator: string | null }[];
  }[];
};
