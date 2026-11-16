import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import {
  ApiError,
  apiRequest,
  apiUrl,
  clearCsrfToken,
  clearLegacyAuthToken,
  logoutBrowserSession,
  setCsrfToken,
  type EvidenceItem,
  type Organization,
  type OrganizationStudioSettings,
  type OrganizationStudioSettingsResponse,
  type ProductionReadiness,
  type ResearchRun,
  type ResearchSource,
  type ResearchSourceRightsReview,
  type ResearchSourceRightsReviewRequest,
  type ReuseRestriction,
  type RightsScope,
  type ProductionRun,
  type MediaArtifact,
  type StoryArtifact,
  type User,
  type YouTubeChannel,
  type YouTubeAnalytics,
  type YouTubeAnalyticsReview,
  type YouTubePendingSelection,
  type YouTubeChannelEditorialProfile,
  type YouTubeChannelSchedule,
  type YouTubePublishingSettings,
} from "./api";
import { formatDate, formatElapsed } from "./format";

type AuthMode = "login" | "register" | "verify";
type WorkspacePage = "overview" | "channels" | "research" | "studio" | "settings";
type SettingsSection = "workspace" | "services" | "publishing" | "channel";
const YOUTUBE_ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly";
const YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload";
const YOUTUBE_VIDEO_WRITE_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl";
const kokoroVoiceChoices = [
  ["af_heart", "Heart · American English"],
  ["af_bella", "Bella · American English"],
  ["af_nicole", "Nicole · American English"],
  ["af_sarah", "Sarah · American English"],
  ["af_sky", "Sky · American English"],
  ["af_alloy", "Alloy · American English"],
  ["af_aoede", "Aoede · American English"],
  ["af_jessica", "Jessica · American English"],
  ["af_kore", "Kore · American English"],
  ["af_nova", "Nova · American English"],
  ["af_river", "River · American English"],
  ["am_adam", "Adam · American English"],
  ["am_michael", "Michael · American English"],
  ["am_echo", "Echo · American English"],
  ["am_eric", "Eric · American English"],
  ["am_fenrir", "Fenrir · American English"],
  ["am_liam", "Liam · American English"],
  ["am_onyx", "Onyx · American English"],
  ["am_puck", "Puck · American English"],
  ["bf_alice", "Alice · British English"],
  ["bf_emma", "Emma · British English"],
  ["bf_isabella", "Isabella · British English"],
  ["bf_lily", "Lily · British English"],
  ["bm_daniel", "Daniel · British English"],
  ["bm_fable", "Fable · British English"],
  ["bm_george", "George · British English"],
  ["bm_lewis", "Lewis · British English"],
] as const;
const qwenVoiceChoices = [["Ryan", "Ryan · English"], ["Aiden", "Aiden · English"]] as const;
const nichePresets = [
  "AI, GPU technology and robotics",
  "Artificial intelligence",
  "GPU and semiconductor technology",
  "Robotics and automation",
  "Consumer technology",
  "Software and developer tools",
  "Space and science",
  "Custom niche…",
] as const;

function defaultVoiceForProvider(provider: string | null | undefined): string | null {
  if (provider === "kokoro") return "af_heart";
  if (provider === "qwen3_tts") return "Ryan";
  return null;
}

function createIdempotencyKey(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

const workspacePageCopy: Record<WorkspacePage, { title: string; eyebrow: string; description: string }> = {
  overview: {
    title: "Your studio at a glance",
    eyebrow: "WORKSPACE OVERVIEW",
    description: "See what is connected, what is ready, and where to continue.",
  },
  channels: {
    title: "Channels",
    eyebrow: "PUBLISHING SETUP",
    description: "Connect YouTube, review analytics, and manage channel schedules.",
  },
  research: {
    title: "Research desk",
    eyebrow: "FIND THE STORY",
    description: "Gather source material and inspect evidence before writing.",
  },
  studio: {
    title: "Video studio",
    eyebrow: "CREATE AND REVIEW",
    description: "Track scripts, scene production, verification, and review decisions.",
  },
  settings: {
    title: "Settings",
    eyebrow: "STUDIO CONFIGURATION",
    description: "Review providers, verify connections, and configure this local runtime.",
  },
};

const productionStageLabels: Record<string, string> = {
  research_snapshot: "Research snapshot",
  story_generation: "Story writing",
  claim_verification: "Fact checking",
  ai_direction: "Scene direction",
  narration: "Narration",
  visual_generation: "Visuals",
  rendering: "Video render",
  quality_check: "Quality review",
};

function App() {
  const [theme, setTheme] = useState<"light" | "dark">(() => {
    try {
      // Start with the requested clean light workspace after this UI refresh;
      // a new key avoids inheriting the old dark-only visual preference.
      return window.localStorage.getItem("aurora-theme-v2") === "dark" ? "dark" : "light";
    } catch {
      return "light";
    }
  });
  const [activePage, setActivePage] = useState<WorkspacePage>(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.has("youtube") || window.location.pathname.includes("integrations")) return "channels";
    if (window.location.pathname.includes("/settings")) return "settings";
    return "overview";
  });
  const [settingsSection, setSettingsSection] = useState<SettingsSection>("workspace");
  const [user, setUser] = useState<User | null>(null);
  const [workspaceLoading, setWorkspaceLoading] = useState(true);
  const [skipSessionRestore] = useState(() =>
    new URLSearchParams(window.location.search).has("token"),
  );
  const [workspaceRetry, setWorkspaceRetry] = useState(0);
  const channelRequestId = useRef(0);
  const researchRequestId = useRef(0);
  const sourceRequestId = useRef(0);
  const evidenceRequestId = useRef(0);
  const rightsReviewRequestId = useRef(0);
  const productionRequestId = useRef(0);
  const storyArtifactRequestId = useRef(0);
  const mediaArtifactRequestId = useRef(0);
  const publishJobRequestId = useRef(0);
  const [organizations, setOrganizations] = useState<Organization[]>([]);
  const [organizationId, setOrganizationId] = useState("");
  const [channels, setChannels] = useState<YouTubeChannel[]>([]);
  const [analyticsChannel, setAnalyticsChannel] = useState<YouTubeChannel | null>(null);
  const [analyticsData, setAnalyticsData] = useState<YouTubeAnalytics | null>(null);
  const [analyticsLoading, setAnalyticsLoading] = useState(false);
  const [analyticsReviewBusy, setAnalyticsReviewBusy] = useState(false);
  const [analyticsError, setAnalyticsError] = useState("");
  const [productionReadiness, setProductionReadiness] = useState<ProductionReadiness | null>(null);
  const [readinessLoading, setReadinessLoading] = useState(false);
  const [readinessVerifying, setReadinessVerifying] = useState(false);
  const [readinessError, setReadinessError] = useState("");
  const [studioSettings, setStudioSettings] = useState<OrganizationStudioSettings>({
    research_enabled: true,
    video_generation_enabled: true,
    automatic_publishing_enabled: false,
    target_duration_seconds: 30,
    qwen_thinking_mode: "off",
    story_max_output_tokens: null,
    story_timeout_seconds: 300,
    language: "English",
    tone: "Clear, engaging, documentary",
    audience: "Curious general audience",
    additional_rules: [],
  });
  const [studioSettingsRulesText, setStudioSettingsRulesText] = useState("");
  const [studioSettingsLoading, setStudioSettingsLoading] = useState(false);
  const [studioSettingsSaving, setStudioSettingsSaving] = useState(false);
  const [studioSettingsCanEdit, setStudioSettingsCanEdit] = useState(false);
  const [studioSettingsError, setStudioSettingsError] = useState("");
  const [studioSettingsNotice, setStudioSettingsNotice] = useState("");
  const [pendingSelectionId, setPendingSelectionId] = useState("");
  const [pendingSelectionOrganizationId, setPendingSelectionOrganizationId] = useState("");
  const [pendingSelection, setPendingSelection] = useState<YouTubePendingSelection | null>(null);
  const [pendingSelectionLoading, setPendingSelectionLoading] = useState(false);
  const [pendingSelectionBusy, setPendingSelectionBusy] = useState(false);
  const [settingsChannel, setSettingsChannel] = useState<YouTubeChannel | null>(null);
  const [channelSettingsTab, setChannelSettingsTab] = useState<"voice" | "visual" | "captions" | "audience" | "safety" | "publishing">("voice");
  const [channelSettingsLoading, setChannelSettingsLoading] = useState(false);
  const [channelSettingsSaving, setChannelSettingsSaving] = useState(false);
  const [editorialProfile, setEditorialProfile] = useState<YouTubeChannelEditorialProfile["profile"] | null>(null);
  const [editorialProfileVersion, setEditorialProfileVersion] = useState<number | null>(null);
  const [scheduleDraft, setScheduleDraft] = useState({
    enabled: false,
    timezone: "Asia/Kolkata",
    posting_time: "09:00",
    catch_up_policy: "skip" as "skip" | "run_once",
  });
  const [publishingSettings, setPublishingSettings] = useState<YouTubePublishingSettings | null>(null);
  const [publishingDraft, setPublishingDraft] = useState({
    auto_publish_enabled: false,
    privacy_status: "private" as "private" | "unlisted" | "public",
  });
  const [researchRuns, setResearchRuns] = useState<ResearchRun[]>([]);
  const [researchLoading, setResearchLoading] = useState(false);
  const [researchCleanupBusy, setResearchCleanupBusy] = useState(false);
  const [researchSubmitting, setResearchSubmitting] = useState(false);
  const [researchTopic, setResearchTopic] = useState("");
  const [selectedResearchRunId, setSelectedResearchRunId] = useState("");
  const [researchSources, setResearchSources] = useState<ResearchSource[]>([]);
  const [sourcesLoading, setSourcesLoading] = useState(false);
  const [selectedResearchSourceId, setSelectedResearchSourceId] = useState("");
  const [researchEvidence, setResearchEvidence] = useState<EvidenceItem[]>([]);
  const [evidenceLoading, setEvidenceLoading] = useState(false);
  const [rightsReviews, setRightsReviews] = useState<ResearchSourceRightsReview[]>([]);
  const [rightsReviewsLoading, setRightsReviewsLoading] = useState(false);
  const [rightsReviewBusy, setRightsReviewBusy] = useState(false);
  const [rightsReviewError, setRightsReviewError] = useState("");
  const [rightsReviewNotice, setRightsReviewNotice] = useState("");
  const [rightsDecision, setRightsDecision] = useState<"cleared" | "restricted">("restricted");
  const [rightsScope, setRightsScope] = useState<RightsScope>("source_text");
  const [rightsLicenseIds, setRightsLicenseIds] = useState("");
  const [rightsLicenseUrls, setRightsLicenseUrls] = useState("");
  const [rightsHolder, setRightsHolder] = useState("");
  const [rightsCreators, setRightsCreators] = useState("");
  const [rightsAttribution, setRightsAttribution] = useState("");
  const [rightsRestrictions, setRightsRestrictions] = useState<string[]>(["attribution"]);
  const [rightsReviewNotes, setRightsReviewNotes] = useState("");
  const [rightsEvidenceUrls, setRightsEvidenceUrls] = useState("");
  const [researchError, setResearchError] = useState("");
  const [productionRuns, setProductionRuns] = useState<ProductionRun[]>([]);
  const [productionLoading, setProductionLoading] = useState(false);
  const [productionCleanupBusy, setProductionCleanupBusy] = useState(false);
  const [productionQueueBusy, setProductionQueueBusy] = useState(false);
  const [productionChannelId, setProductionChannelId] = useState("");
  const [selectedProductionRunId, setSelectedProductionRunId] = useState("");
  const [storyArtifacts, setStoryArtifacts] = useState<StoryArtifact[]>([]);
  const [mediaArtifacts, setMediaArtifacts] = useState<MediaArtifact[]>([]);
  const [storyArtifactsLoading, setStoryArtifactsLoading] = useState(false);
  const [productionError, setProductionError] = useState("");
  const [productionActionBusy, setProductionActionBusy] = useState(false);
  const [publishActionBusy, setPublishActionBusy] = useState(false);
  const [publishStatusError, setPublishStatusError] = useState("");
  const publishRequestInFlight = useRef(false);
  const [publishJob, setPublishJob] = useState<{
    id: string;
    status: string;
    title: string;
    privacy_status: string;
    bytes_uploaded: number;
    byte_size: number;
    attempt_count: number;
    youtube_processing_status: string | null;
    youtube_video_id: string | null;
    failure_code: string | null;
    failure_message: string | null;
    thumbnail_upload_warning: string | null;
    published_at: string | null;
  } | null>(null);
  const [publishTitle, setPublishTitle] = useState("");
  const [publishDescription, setPublishDescription] = useState("");
  const [publishTags, setPublishTags] = useState("");
  const [publishPrivacy, setPublishPrivacy] = useState<"private" | "unlisted" | "public">("public");
  const [warningOverrideAcknowledged, setWarningOverrideAcknowledged] = useState(false);
  const [warningOverrideReason, setWarningOverrideReason] = useState("");
  const publishMetadataRunId = useRef("");
  const [authMode, setAuthMode] = useState<AuthMode>("login");
  const [authEmail, setAuthEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [verificationToken, setVerificationToken] = useState("");
  const [newOrganizationName, setNewOrganizationName] = useState("");
  const [showCreateOrganization, setShowCreateOrganization] = useState(false);
  const [loading, setLoading] = useState(false);
  const [channelsLoading, setChannelsLoading] = useState(false);
  const [busyChannelId, setBusyChannelId] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    clearLegacyAuthToken();
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try { window.localStorage.setItem("aurora-theme-v2", theme); } catch { /* Storage can be disabled. */ }
  }, [theme]);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(""), 6_000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const selectedOrganization = useMemo(
    () => organizations.find((item) => item.id === organizationId) ?? null,
    [organizations, organizationId],
  );

  const selectedResearchSource = useMemo(
    () => researchSources.find((source) => source.id === selectedResearchSourceId) ?? null,
    [researchSources, selectedResearchSourceId],
  );
  const selectedResearchRun = useMemo(
    () => researchRuns.find((run) => run.id === selectedResearchRunId) ?? null,
    [researchRuns, selectedResearchRunId],
  );

  const clearMessages = () => {
    setNotice("");
    setError("");
  };

  const loadWorkspace = useCallback(async () => {
    const profile = await apiRequest<User>("/auth/browser-session");

    if (!profile.email_verified) {
      return { profile, organizations: [] as Organization[] };
    }

    const organizations = await apiRequest<Organization[]>("/organizations");
    return { profile, organizations };
  }, []);

  const loadChannels = useCallback(async () => {
    const requestId = ++channelRequestId.current;
    if (!user || !organizationId) {
      setChannels([]);
      setChannelsLoading(false);
      return;
    }
    setChannels([]);
    setChannelsLoading(true);
    try {
      const data = await apiRequest<YouTubeChannel[]>(
        `/organizations/${organizationId}/youtube-channels`,
      );
      if (requestId === channelRequestId.current) setChannels(data);
    } catch (err) {
      if (requestId === channelRequestId.current) {
        setError(err instanceof Error ? err.message : "Unable to load channels.");
      }
    } finally {
      if (requestId === channelRequestId.current) setChannelsLoading(false);
    }
  }, [organizationId, user]);

  async function loadChannelAnalytics(channel: YouTubeChannel) {
    if (!organizationId) return;
    setAnalyticsChannel(channel);
    setAnalyticsData(null);
    setAnalyticsError("");
    setAnalyticsLoading(true);
    // Ask through the last calendar day. YouTube Analytics can lag by 48–72h;
    // keep those days visible as pending instead of silently shortening the
    // report window to hide them.
    const end = new Date();
    end.setDate(end.getDate() - 1);
    const start = new Date(end);
    start.setDate(start.getDate() - 27);
    const toCalendarDate = (date: Date) => [
      date.getFullYear(),
      String(date.getMonth() + 1).padStart(2, "0"),
      String(date.getDate()).padStart(2, "0"),
    ].join("-");
    const endDate = toCalendarDate(end);
    const startDate = toCalendarDate(start);
    try {
      const result = await apiRequest<YouTubeAnalytics>(
        `/organizations/${organizationId}/youtube-channels/${channel.id}/analytics?startDate=${startDate}&endDate=${endDate}`,
      );
      setAnalyticsData(result);
    } catch (err) {
      setAnalyticsError(err instanceof Error ? err.message : "Unable to load channel analytics.");
    } finally {
      setAnalyticsLoading(false);
    }
  }

  async function decideAnalyticsReview(decision: "approved" | "dismissed") {
    const review = analyticsData?.comparison?.review;
    if (!organizationId || !analyticsChannel || !review || analyticsReviewBusy) return;
    setAnalyticsReviewBusy(true);
    setAnalyticsError("");
    try {
      const updated = await apiRequest<YouTubeAnalyticsReview>(
        `/organizations/${organizationId}/youtube-channels/${analyticsChannel.id}/analytics-reviews/${review.id}`,
        { method: "POST", body: JSON.stringify({ decision }) },
      );
      setAnalyticsData((current) => current?.comparison
        ? { ...current, comparison: { ...current.comparison, review: updated } }
        : current);
    } catch (err) {
      setAnalyticsError(err instanceof Error ? err.message : "Unable to save analytics review.");
    } finally {
      setAnalyticsReviewBusy(false);
    }
  }

  const loadProductionReadiness = useCallback(async () => {
    if (!user || !organizationId) {
      setProductionReadiness(null);
      return;
    }
    setReadinessLoading(true);
    setReadinessError("");
    try {
      const result = await apiRequest<ProductionReadiness>(
        `/organizations/${organizationId}/production-readiness`,
      );
      setProductionReadiness(result);
    } catch (err) {
      setReadinessError(err instanceof Error ? err.message : "Unable to check production readiness.");
    } finally {
      setReadinessLoading(false);
    }
  }, [organizationId, user]);

  async function verifyProductionReadiness() {
    if (!organizationId || readinessVerifying) return;
    setReadinessVerifying(true);
    setReadinessError("");
    try {
      const result = await apiRequest<ProductionReadiness>(
        `/organizations/${organizationId}/production-readiness/verify`,
        { method: "POST" },
      );
      setProductionReadiness(result);
    } catch (err) {
      setReadinessError(err instanceof Error ? err.message : "Unable to verify provider connections.");
    } finally {
      setReadinessVerifying(false);
    }
  }

  useEffect(() => {
    void loadProductionReadiness();
  }, [loadProductionReadiness]);

  const loadStudioSettings = useCallback(async () => {
    if (!user || !organizationId) return;
    setStudioSettingsLoading(true);
    setStudioSettingsError("");
    try {
      const result = await apiRequest<OrganizationStudioSettingsResponse>(
        `/organizations/${organizationId}/studio-settings`,
      );
      setStudioSettings(result.settings);
      setStudioSettingsCanEdit(result.can_edit);
      setStudioSettingsRulesText(result.settings.additional_rules.join("\n"));
    } catch (err) {
      setStudioSettingsError(
        err instanceof Error ? err.message : "Unable to load workspace settings.",
      );
    } finally {
      setStudioSettingsLoading(false);
    }
  }, [organizationId, user]);

  useEffect(() => {
    void loadStudioSettings();
  }, [loadStudioSettings]);

  async function saveStudioSettings(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!organizationId || studioSettingsSaving) return;
    setStudioSettingsSaving(true);
    setStudioSettingsError("");
    setStudioSettingsNotice("");
    try {
      const result = await apiRequest<OrganizationStudioSettingsResponse>(
        `/organizations/${organizationId}/studio-settings`,
        {
          method: "PUT",
          body: JSON.stringify({
            ...studioSettings,
            additional_rules: studioSettingsRulesText
              .split("\n")
              .map((rule) => rule.trim())
              .filter(Boolean),
          }),
        },
      );
      setStudioSettings(result.settings);
      setStudioSettingsCanEdit(result.can_edit);
      setStudioSettingsRulesText(result.settings.additional_rules.join("\n"));
      setStudioSettingsNotice("Workspace settings saved. New production runs will use these defaults.");
    } catch (err) {
      setStudioSettingsError(
        err instanceof Error ? err.message : "Unable to save workspace settings.",
      );
    } finally {
      setStudioSettingsSaving(false);
    }
  }

  const loadResearchRuns = useCallback(async () => {
    const requestId = ++researchRequestId.current;
    if (!user || !organizationId) {
      setResearchRuns([]);
      setSelectedResearchRunId("");
      setResearchSources([]);
      setSelectedResearchSourceId("");
      setResearchEvidence([]);
      setResearchLoading(false);
      return;
    }
    setResearchLoading(true);
    setResearchError("");
    try {
      const data = await apiRequest<ResearchRun[]>(
        `/organizations/${organizationId}/research-runs`,
      );
      if (requestId !== researchRequestId.current) return;
      setResearchRuns(data);
      setSelectedResearchRunId((current) =>
        data.some((run) => run.id === current) ? current : data[0]?.id ?? "",
      );
    } catch (err) {
      if (requestId === researchRequestId.current) {
        setResearchError(err instanceof Error ? err.message : "Unable to load research runs.");
      }
    } finally {
      if (requestId === researchRequestId.current) setResearchLoading(false);
    }
  }, [organizationId, user]);

  async function cleanFailedResearchRuns() {
    if (!user || !organizationId || researchCleanupBusy) return;
    if (!window.confirm("Remove failed, unreferenced research runs and their source history? Active, completed, and production-linked research will be kept.")) return;
    setResearchCleanupBusy(true);
    setResearchError("");
    try {
      const result = await apiRequest<{ deleted_count: number }>(
        `/organizations/${organizationId}/research-runs/cleanup`,
        { method: "POST" },
      );
      setNotice(result.deleted_count ? `Cleaned ${result.deleted_count} failed research run${result.deleted_count === 1 ? "" : "s"}.` : "No safe-to-delete failed research runs were found.");
      await loadResearchRuns();
    } catch (err) {
      setResearchError(err instanceof Error ? err.message : "Unable to clean failed research runs.");
    } finally {
      setResearchCleanupBusy(false);
    }
  }

  const loadResearchSources = useCallback(async () => {
    const requestId = ++sourceRequestId.current;
    if (!user || !organizationId || !selectedResearchRunId) {
      setResearchSources([]);
      setSelectedResearchSourceId("");
      setResearchEvidence([]);
      setSourcesLoading(false);
      return;
    }
    setSourcesLoading(true);
    setResearchError("");
    try {
      const data = await apiRequest<ResearchSource[]>(
        `/organizations/${organizationId}/research-runs/${selectedResearchRunId}/sources`,
      );
      if (requestId !== sourceRequestId.current) return;
      setResearchSources(data);
      setSelectedResearchSourceId((current) =>
        data.some((source) => source.id === current) ? current : data[0]?.id ?? "",
      );
    } catch (err) {
      if (requestId === sourceRequestId.current) {
        setResearchError(err instanceof Error ? err.message : "Unable to load research sources.");
      }
    } finally {
      if (requestId === sourceRequestId.current) setSourcesLoading(false);
    }
  }, [organizationId, selectedResearchRunId, user]);

  const loadResearchEvidence = useCallback(async () => {
    const requestId = ++evidenceRequestId.current;
    const sourceMatchesRun = researchSources.some(
      (source) => source.id === selectedResearchSourceId && source.research_run_id === selectedResearchRunId,
    );
    if (!user || !organizationId || !selectedResearchRunId || !sourceMatchesRun) {
      setResearchEvidence([]);
      setEvidenceLoading(false);
      return;
    }
    setEvidenceLoading(true);
    setResearchError("");
    try {
      const data = await apiRequest<EvidenceItem[]>(
        `/organizations/${organizationId}/research-runs/${selectedResearchRunId}/sources/${selectedResearchSourceId}/evidence`,
      );
      if (requestId === evidenceRequestId.current) setResearchEvidence(data);
    } catch (err) {
      if (requestId === evidenceRequestId.current) {
        setResearchError(err instanceof Error ? err.message : "Unable to load evidence excerpts.");
      }
    } finally {
      if (requestId === evidenceRequestId.current) setEvidenceLoading(false);
    }
  }, [organizationId, researchSources, selectedResearchRunId, selectedResearchSourceId, user]);

  useEffect(() => {
    void loadResearchRuns();
  }, [loadResearchRuns]);

  useEffect(() => {
    void loadResearchSources();
  }, [loadResearchSources]);

  useEffect(() => {
    void loadResearchEvidence();
  }, [loadResearchEvidence]);

  const loadRightsReviews = useCallback(async () => {
    const requestId = ++rightsReviewRequestId.current;
    const sourceMatchesRun = researchSources.some(
      (source) => source.id === selectedResearchSourceId && source.research_run_id === selectedResearchRunId,
    );
    if (!user || !organizationId || !selectedResearchRunId || !sourceMatchesRun) {
      setRightsReviews([]);
      setRightsReviewsLoading(false);
      return;
    }
    setRightsReviewsLoading(true);
    setRightsReviewError("");
    try {
      const data = await apiRequest<ResearchSourceRightsReview[]>(
        `/organizations/${organizationId}/research-runs/${selectedResearchRunId}/sources/${selectedResearchSourceId}/rights-reviews`,
      );
      if (requestId === rightsReviewRequestId.current) setRightsReviews(data);
    } catch (err) {
      if (requestId === rightsReviewRequestId.current) {
        setRightsReviews([]);
        setRightsReviewError(
          err instanceof Error ? err.message : "Unable to load rights review history.",
        );
      }
    } finally {
      if (requestId === rightsReviewRequestId.current) setRightsReviewsLoading(false);
    }
  }, [organizationId, researchSources, selectedResearchRunId, selectedResearchSourceId, user]);

  useEffect(() => {
    const sourceMatchesRun = researchSources.some(
      (source) => source.id === selectedResearchSourceId && source.research_run_id === selectedResearchRunId,
    );
    if (selectedResearchSourceId && !sourceMatchesRun) {
      setSelectedResearchSourceId("");
      setResearchEvidence([]);
      setRightsReviews([]);
      setResearchError("");
      setRightsReviewError("");
    }
  }, [researchSources, selectedResearchRunId, selectedResearchSourceId]);

  useEffect(() => {
    void loadRightsReviews();
  }, [loadRightsReviews]);

  useEffect(() => {
    setRightsReviewError("");
    setRightsReviewNotice("");
    setRightsDecision("restricted");
    setRightsScope(selectedResearchSource?.rights_metadata.scope ?? "source_text");
    setRightsLicenseIds(selectedResearchSource?.rights_metadata.license_ids.join(", ") ?? "");
    setRightsLicenseUrls(selectedResearchSource?.rights_metadata.license_urls.join("\\n") ?? "");
    setRightsHolder(selectedResearchSource?.rights_metadata.rights_holder ?? "");
    setRightsCreators(selectedResearchSource?.rights_metadata.creators.join(", ") ?? "");
    setRightsAttribution(selectedResearchSource?.rights_metadata.attribution_text ?? "");
    setRightsRestrictions(
      selectedResearchSource?.rights_metadata.reuse_restrictions.length
        ? [...selectedResearchSource.rights_metadata.reuse_restrictions]
        : ["attribution"],
    );
    setRightsReviewNotes("");
    setRightsEvidenceUrls("");
  }, [selectedResearchSourceId]);


  const loadProductionRuns = useCallback(async (silent = false) => {
    const requestId = ++productionRequestId.current;
    if (!user || !organizationId) {
      setProductionRuns([]);
      setSelectedProductionRunId("");
      setStoryArtifacts([]);
      setProductionLoading(false);
      return;
    }
    if (!silent) setProductionLoading(true);
    if (!silent) setProductionError("");
    try {
      const data = await apiRequest<ProductionRun[]>(
        `/organizations/${organizationId}/production-runs?limit=50`,
      );
      if (requestId !== productionRequestId.current) return;
      setProductionRuns(data);
      setSelectedProductionRunId((current) =>
        data.some((run) => run.id === current) ? current : data[0]?.id ?? "",
      );
    } catch (err) {
      if (requestId === productionRequestId.current) {
        setProductionError(err instanceof Error ? err.message : "Unable to load production runs.");
      }
    } finally {
      if (requestId === productionRequestId.current && !silent) setProductionLoading(false);
    }
  }, [organizationId, user]);

  async function cleanFailedProductionRuns() {
    if (!user || !organizationId || productionCleanupBusy) return;
    if (!window.confirm("Remove failed, cancelled, and rejected production attempts and their generated files? Active, review, approved, and upload-linked runs will be kept.")) return;
    setProductionCleanupBusy(true);
    setProductionError("");
    try {
      const result = await apiRequest<{ deleted_count: number; skipped_count: number }>(
        `/organizations/${organizationId}/production-runs/cleanup`,
        { method: "POST" },
      );
      const suffix = result.skipped_count ? ` ${result.skipped_count} upload-linked run${result.skipped_count === 1 ? " was" : "s were"} kept.` : "";
      setNotice(result.deleted_count ? `Cleaned ${result.deleted_count} old production attempt${result.deleted_count === 1 ? "" : "s"}.${suffix}` : "No safe-to-delete production attempts were found.");
      await loadProductionRuns();
    } catch (err) {
      setProductionError(err instanceof Error ? err.message : "Unable to clean production history.");
    } finally {
      setProductionCleanupBusy(false);
    }
  }

  const loadStoryArtifacts = useCallback(async (silent = false) => {
    const requestId = ++storyArtifactRequestId.current;
    if (!user || !organizationId || !selectedProductionRunId) {
      setStoryArtifacts([]);
      setStoryArtifactsLoading(false);
      return;
    }
    if (!silent) setStoryArtifactsLoading(true);
    if (!silent) setProductionError("");
    try {
      const data = await apiRequest<StoryArtifact[]>(
        `/organizations/${organizationId}/production-runs/${selectedProductionRunId}/story-artifacts`,
      );
      if (requestId === storyArtifactRequestId.current) setStoryArtifacts(data);
    } catch (err) {
      if (requestId === storyArtifactRequestId.current && !silent) {
        setStoryArtifacts([]);
        setProductionError(err instanceof Error ? err.message : "Unable to load story artifacts.");
      }
    } finally {
      if (requestId === storyArtifactRequestId.current && !silent) setStoryArtifactsLoading(false);
    }
  }, [organizationId, selectedProductionRunId, user]);

  const loadMediaArtifacts = useCallback(async (silent = false) => {
    const requestId = ++mediaArtifactRequestId.current;
    if (!user || !organizationId || !selectedProductionRunId) {
      setMediaArtifacts([]);
      return;
    }
    try {
      const data = await apiRequest<MediaArtifact[]>(
        `/organizations/${organizationId}/production-runs/${selectedProductionRunId}/media-artifacts`,
      );
      if (requestId === mediaArtifactRequestId.current) setMediaArtifacts(data);
    } catch (err) {
      if (requestId === mediaArtifactRequestId.current && !silent) {
        setMediaArtifacts([]);
        setProductionError(err instanceof Error ? err.message : "Unable to load media artifacts.");
      }
    }
  }, [organizationId, selectedProductionRunId, user]);

  const loadProductionPublishJob = useCallback(async () => {
    const requestId = ++publishJobRequestId.current;
    if (!user || !organizationId || !selectedProductionRunId) {
      setPublishJob(null);
      setPublishStatusError("");
      return;
    }
    try {
      const jobs = await apiRequest<Array<{
        id: string; production_run_id: string; status: string; title: string;
        privacy_status: string; bytes_uploaded: number; byte_size: number; attempt_count: number;
        youtube_processing_status: string | null; youtube_video_id: string | null; failure_code: string | null;
        failure_message: string | null; thumbnail_upload_warning: string | null; published_at: string | null;
      }>>(`/organizations/${organizationId}/youtube-publish-jobs?limit=100`);
      const job = jobs.find((item) => item.production_run_id === selectedProductionRunId);
      if (requestId === publishJobRequestId.current) {
        setPublishStatusError("");
        setPublishJob(job ? { ...job, privacy_status: job.privacy_status } : null);
      }
    } catch {
      if (requestId === publishJobRequestId.current) {
        setPublishStatusError("Upload status could not refresh. Showing the last confirmed status; Aurora will retry automatically.");
      }
    }
  }, [organizationId, selectedProductionRunId, user]);

  const selectedProductionRun = useMemo(
    () => productionRuns.find((run) => run.id === selectedProductionRunId) ?? null,
    [productionRuns, selectedProductionRunId],
  );
  const selectedRunChannel = useMemo(
    () => channels.find((channel) => channel.id === selectedProductionRun?.youtube_channel_id),
    [channels, selectedProductionRun?.youtube_channel_id],
  );
  const productionHardBlocker = useMemo(() => {
    if (!selectedProductionRun) return null;
    if (!selectedRunChannel?.can_publish) {
      const hasUploadScope = selectedRunChannel?.granted_scopes.includes(YOUTUBE_UPLOAD_SCOPE) ?? false;
      const hasVideoWriteScope = selectedRunChannel?.granted_scopes.includes(YOUTUBE_VIDEO_WRITE_SCOPE) ?? false;
      return hasUploadScope && !hasVideoWriteScope
        ? "YouTube upload access is present, but this channel has not granted video-update permission. Reconnect publishing access before creating another upload job."
        : "This channel needs YouTube upload and video-update permissions. Reconnect it with publishing access before creating another upload job.";
    }
    if (selectedProductionRun.youtube_channel_id !== productionChannelId) {
      return "This run is bound to a different channel. Create a new production for the selected demo channel before approval and upload.";
    }
    const qualityStage = selectedProductionRun.stages.find(
      (stage) => stage.stage_key === "quality_check",
    );
    if (!qualityStage || qualityStage.status !== "completed") {
      return "Complete the media quality check before approving this video.";
    }
    const manifest = qualityStage.output_manifest;
    const qualityReport = manifest.quality_report;
    if (
      !qualityReport ||
      typeof qualityReport !== "object" ||
      Array.isArray(qualityReport) ||
      (qualityReport as Record<string, unknown>).is_valid !== true
    ) {
      return "The video has not passed its technical quality check.";
    }
    if (Array.isArray(manifest.media_manifest_flags) && manifest.media_manifest_flags.length) {
      return "The media manifest has integrity problems. Regenerate the affected media before upload.";
    }
    const report = qualityReport as Record<string, unknown>;
    if (Array.isArray(report.issues) && report.issues.length) {
      return "Technical video checks found problems. Fix and regenerate the video before upload.";
    }
    return null;
  }, [productionChannelId, selectedProductionRun, selectedRunChannel]);
  const uploadReadyRunForSelectedChannel = useMemo(
    () => productionRuns.find(
      (run) => run.id !== selectedProductionRun?.id
        && run.youtube_channel_id === productionChannelId
        && ["awaiting_review", "approved"].includes(run.status),
    ) ?? null,
    [productionChannelId, productionRuns, selectedProductionRun?.id],
  );
  const productionEditorialWarnings = useMemo(() => {
    const stage = selectedProductionRun?.stages.find((item) => item.stage_key === "quality_check");
    const manifest = stage?.output_manifest;
    if (!manifest) return [] as Array<{ kind: string; detail: string }>;
    const result: Array<{ kind: string; detail: string }> = [];
    for (const [kind, values] of [["Factual review", manifest.claim_flags], ["Source rights", manifest.rights_flags]] as const) {
      if (!Array.isArray(values)) continue;
      for (const value of values) {
        const warning = value && typeof value === "object" ? value as Record<string, unknown> : {};
        const source = typeof warning.source_id === "string" ? ` · source ${warning.source_id.slice(0, 8)}` : "";
        const reason = typeof warning.reason === "string" ? warning.reason.replaceAll("_", " ") : "review required";
        const restrictions = Array.isArray(warning.reuse_restrictions) ? ` (${warning.reuse_restrictions.map(String).join(", ")})` : "";
        result.push({ kind, detail: `${reason}${restrictions}${source}` });
      }
    }
    return result;
  }, [selectedProductionRun]);
  const productionApprovalBlocker = productionHardBlocker ?? (
    productionEditorialWarnings.length
      ? "Factual or source-rights warnings need review. Use the separate audited override only if you have reviewed and accept them."
      : null
  );
  const canOverrideEditorialWarnings = Boolean(
    productionHardBlocker === null && productionEditorialWarnings.length > 0,
  );

  useEffect(() => {
    if (!selectedProductionRunId || publishMetadataRunId.current === selectedProductionRunId) return;
    const verified = storyArtifacts.find(
      (artifact) => artifact.production_run_id === selectedProductionRunId && artifact.artifact_type === "story_verified",
    );
    if (!verified) return;
    publishMetadataRunId.current = selectedProductionRunId;
    const title = verified.payload.title.slice(0, 100);
    setPublishTitle(title);
    setPublishDescription(`${verified.payload.hook}\n\n${verified.payload.narration}`.slice(0, 5000));
    const tags = [...new Set(title.toLowerCase().split(/[^a-z0-9]+/).filter((word) => word.length > 2))].slice(0, 8);
    setPublishTags(tags.join(", "));
    setWarningOverrideAcknowledged(false);
    setWarningOverrideReason("");
  }, [selectedProductionRunId, storyArtifacts]);

  useEffect(() => {
    setProductionRuns([]);
    setSelectedProductionRunId("");
    setStoryArtifacts([]);
    setMediaArtifacts([]);
  }, [organizationId]);

  useEffect(() => {
    void loadProductionRuns();
  }, [loadProductionRuns]);

  useEffect(() => {
    if (!selectedProductionRun || !["queued", "generating"].includes(selectedProductionRun.status)) return;
    const timer = window.setInterval(() => {
      void loadProductionRuns(true);
      void loadStoryArtifacts(true);
      void loadMediaArtifacts(true);
    }, 15_000);
    return () => window.clearInterval(timer);
  }, [loadMediaArtifacts, loadProductionRuns, loadStoryArtifacts, selectedProductionRun]);

  useEffect(() => {
    void loadStoryArtifacts();
  }, [loadStoryArtifacts]);

  useEffect(() => {
    void loadMediaArtifacts();
  }, [loadMediaArtifacts]);

  useEffect(() => {
    void loadProductionPublishJob();
  }, [loadProductionPublishJob]);

  useEffect(() => {
    const channelId = selectedProductionRun?.youtube_channel_id;
    let active = true;
    if (!user || !organizationId || !channelId) {
      setPublishPrivacy("private");
      return () => {
        active = false;
      };
    }
    void apiRequest<YouTubePublishingSettings>(
      `/organizations/${organizationId}/youtube-channels/${channelId}/publishing-settings`,
    )
      .then((settings) => {
        if (active) setPublishPrivacy(settings.auto_publish_privacy_status);
      })
      .catch(() => {
        if (active) setPublishPrivacy("private");
      });
    return () => {
      active = false;
    };
  }, [organizationId, selectedProductionRun?.youtube_channel_id, user]);

  useEffect(() => {
    const publishingIsActive = publishJob
      ? ["queued", "initiating", "uploading", "processing", "uploaded", "reconciliation_required"].includes(publishJob.status)
      : selectedProductionRun?.status === "approved";
    if (!publishingIsActive) return;
    const timer = window.setInterval(() => void loadProductionPublishJob(), 3_000);
    return () => window.clearInterval(timer);
  }, [loadProductionPublishJob, publishJob, selectedProductionRun?.status]);

  useEffect(() => {
    const verified = storyArtifacts.find((artifact) => artifact.artifact_type === "story_verified");
    if (!verified) return;
    setPublishTitle(verified.payload.title.slice(0, 100));
    setPublishDescription(`${verified.payload.hook}\n\n${verified.payload.narration}`.slice(0, 5000));
    setPublishTags("");
  }, [selectedProductionRunId, storyArtifacts]);

  useEffect(() => {
    if (skipSessionRestore) {
      setWorkspaceLoading(false);
      return;
    }
    let active = true;
    setWorkspaceLoading(true);
    loadWorkspace()
      .then(({ profile, organizations: orgs }) => {
        // Do not let a response from an obsolete session overwrite the
        // workspace belonging to a newer session or a signed-out view.
        if (!active) return;
        setError("");
        setUser(profile);
        setOrganizations(orgs);
        setOrganizationId((current) =>
          orgs.some((org) => org.id === current) ? current : orgs[0]?.id ?? "",
        );
      })
      .catch((err: unknown) => {
        if (!active) return;
        if (err instanceof ApiError && err.status === 401) {
          clearCsrfToken();
          setUser(null);
          setOrganizations([]);
          setOrganizationId("");
          setChannels([]);
          setError("");
          return;
        }
        setError(
          err instanceof Error
            ? err.message
            : "Unable to load your workspace. Please try again.",
        );
      })
      .finally(() => {
        if (active) setWorkspaceLoading(false);
      });
    return () => {
      active = false;
    };
  }, [loadWorkspace, skipSessionRestore, workspaceRetry]);

  useEffect(() => {
    if (user && organizationId) void loadChannels();
  }, [loadChannels, organizationId, user]);

  useEffect(() => {
    const publishableChannel =
      channels.find((channel) => channel.can_publish) ??
      channels[0];
    setProductionChannelId((current) =>
      channels.some((channel) => channel.id === current)
        ? current
        : publishableChannel?.id ?? "",
    );
  }, [channels]);
  useEffect(() => {
    if (!pendingSelectionOrganizationId || organizations.length === 0) return;
    if (organizations.some((org) => org.id === pendingSelectionOrganizationId)) {
      setOrganizationId(pendingSelectionOrganizationId);
    } else {
      setError("The workspace for this pending channel selection is not available to your account.");
      setPendingSelectionId("");
      setPendingSelectionOrganizationId("");
    }
  }, [organizations, pendingSelectionOrganizationId]);

  useEffect(() => {
    if (!user || !pendingSelectionId || !organizationId ||
        organizationId !== pendingSelectionOrganizationId) {
      setPendingSelection(null);
      return;
    }
    let active = true;
    setPendingSelectionLoading(true);
    apiRequest<YouTubePendingSelection>(
      `/organizations/${organizationId}/youtube-pending-selections/${pendingSelectionId}`,
    ).then((result) => {
      if (active) setPendingSelection(result);
    }).catch((err: unknown) => {
      if (active) setError(err instanceof Error ? err.message : "Unable to load pending channel choices.");
    }).finally(() => {
      if (active) setPendingSelectionLoading(false);
    });
    return () => { active = false; };
  }, [organizationId, pendingSelectionId, pendingSelectionOrganizationId, user]);


  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const result = params.get("youtube");
    const attemptId = params.get("attempt_id");
    const pendingOrganizationId = params.get("organization_id");
    const emailToken = params.get("token");

    if (emailToken) {
      void signOut();
      setAuthMode("verify");
      setVerificationToken(emailToken);
      setNotice("Your email verification link is ready. Confirm to continue.");
      window.history.replaceState({}, document.title, window.location.pathname);
      return;
    }

    if (result === "connected") {
      setNotice("YouTube channel authorization updated successfully.");
      window.history.replaceState({}, document.title, window.location.pathname);
    } else if (result === "select" && attemptId && pendingOrganizationId) {
      setPendingSelectionId(attemptId);
      setPendingSelectionOrganizationId(pendingOrganizationId);
      setNotice("Google authorization succeeded. Select the exact YouTube channel to connect.");
      window.history.replaceState({}, document.title, window.location.pathname);
    } else if (result === "error") {
      setError("YouTube authorization was not completed. You can try again.");
      window.history.replaceState({}, document.title, window.location.pathname);
    }
  }, []);

  async function handleAuthSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    clearMessages();
    setLoading(true);
    try {
      if (authMode === "register") {
        await apiRequest<User>("/auth/register", {
          method: "POST",
          body: JSON.stringify({
            email: authEmail,
            display_name: displayName,
            password,
          }),
        });
        setAuthMode("verify");
        setNotice("Account created. Verify your email before signing in.");
      } else if (authMode === "verify") {
        const result = await apiRequest<{ message: string; email: string }>(
          "/auth/email-verification/verify",
          {
            method: "POST",
            body: JSON.stringify({ token: verificationToken.trim() }),
          },
        );
        setAuthEmail(result.email);
        setAuthMode("login");
        setNotice("Email verified. You can now sign in.");
      } else {
        const result = await apiRequest<{ user: User; csrf_token: string }>(
          "/auth/browser-session/login",
          {
            method: "POST",
            body: JSON.stringify({ email: authEmail, password }),
          },
        );
        setCsrfToken(result.csrf_token);
        setUser(result.user);
        setWorkspaceLoading(true);
        setWorkspaceRetry((value) => value + 1);
        setPassword("");
        setNotice("Signed in successfully.");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Authentication failed.");
    } finally {
      setLoading(false);
    }
  }

  async function resendVerification() {
    if (!user) return;
    clearMessages();
    setLoading(true);
    try {
      const result = await apiRequest<{ message: string }>(
        "/auth/email-verification/resend",
        { method: "POST" },
      );
      setNotice(result.message);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to resend email.");
    } finally {
      setLoading(false);
    }
  }

  async function createOrganization(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!user) return;
    clearMessages();
    setLoading(true);
    try {
      const organization = await apiRequest<Organization>("/organizations", {
        method: "POST",
        body: JSON.stringify({ name: newOrganizationName }),
      });
      setOrganizations((current) => [...current, organization]);
      setOrganizationId(organization.id);
      setNewOrganizationName("");
      setShowCreateOrganization(false);
      setNotice("Workspace created.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to create workspace.");
    } finally {
      setLoading(false);
    }
  }

  async function createResearchRun(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!user || !organizationId || !researchTopic.trim()) return;
    setResearchSubmitting(true);
    setResearchError("");
    clearMessages();
    try {
      const created = await apiRequest<ResearchRun>(
        `/organizations/${organizationId}/research-runs`,
        {
          method: "POST",
          body: JSON.stringify({ topic: researchTopic.trim() }),
        },
      );
      setResearchTopic("");
      setResearchRuns((current) => [created, ...current.filter((run) => run.id !== created.id)]);
      setSelectedResearchRunId(created.id);
      setNotice("Research run queued. It will progress when the research worker is running.");
    } catch (err) {
      setResearchError(err instanceof Error ? err.message : "Unable to create research run.");
    } finally {
      setResearchSubmitting(false);
    }
  }

  async function createProductionFromResearch() {
    if (!organizationId || !selectedResearchRun || selectedResearchRun.status !== "completed") return;
    if (!productionChannelId) {
      setResearchError("Connect a YouTube channel before creating a video production.");
      return;
    }
    setProductionQueueBusy(true);
    setResearchError("");
    clearMessages();
    try {
      const created = await apiRequest<ProductionRun>(
        `/organizations/${organizationId}/production-runs`,
        {
          method: "POST",
          headers: { "Idempotency-Key": createIdempotencyKey() },
          body: JSON.stringify({
            research_run_id: selectedResearchRun.id,
            youtube_channel_id: productionChannelId,
          }),
        },
      );
      setProductionRuns((current) => [created, ...current.filter((run) => run.id !== created.id)]);
      setSelectedProductionRunId(created.id);
      setActivePage("studio");
      setNotice("Video production queued. Review the script, media rights, and final video before any upload.");
      void loadProductionRuns();
    } catch (err) {
      setResearchError(err instanceof Error ? err.message : "Unable to queue video production.");
    } finally {
      setProductionQueueBusy(false);
    }
  }

  async function recreateProductionForSelectedChannel() {
    if (!organizationId || !selectedProductionRun || !productionChannelId) return;
    const target = channels.find((channel) => channel.id === productionChannelId);
    if (!target?.can_publish) {
      setProductionError("Choose a connected channel with full YouTube publishing access first.");
      return;
    }
    setProductionQueueBusy(true);
    setProductionError("");
    clearMessages();
    try {
      const created = await apiRequest<ProductionRun>(
        `/organizations/${organizationId}/production-runs`,
        {
          method: "POST",
          headers: { "Idempotency-Key": createIdempotencyKey() },
          body: JSON.stringify({
            research_run_id: selectedProductionRun.research_run_id,
            youtube_channel_id: target.id,
          }),
        },
      );
      setProductionRuns((current) => [created, ...current.filter((run) => run.id !== created.id)]);
      setSelectedProductionRunId(created.id);
      setNotice(`A new draft is queued for ${target.title}. The original run and its artifacts are unchanged.`);
      void loadProductionRuns();
    } catch (err) {
      setProductionError(err instanceof Error ? err.message : "Unable to create a draft for this channel.");
    } finally {
      setProductionQueueBusy(false);
    }
  }


  async function submitSourceRightsReview(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!user || !organizationId || !selectedResearchRunId || !selectedResearchSource) return;

    const splitValues = (value: string) =>
      value.split(/[\\n,]/).map((item) => item.trim()).filter(Boolean);
    const licenseIds = splitValues(rightsLicenseIds);
    const licenseUrls = splitValues(rightsLicenseUrls);
    const additionalEvidenceUrls = splitValues(rightsEvidenceUrls);
    const evidenceUrls = Array.from(
      new Set([
        ...additionalEvidenceUrls,
        ...licenseUrls,
        ...(selectedResearchSource.rights_metadata.source_revision_url
          ? [selectedResearchSource.rights_metadata.source_revision_url]
          : []),
      ]),
    );

    if (evidenceUrls.length === 0) {
      setRightsReviewError("Add at least one HTTPS evidence URL before submitting.");
      return;
    }
    if (rightsDecision === "cleared") {
      if (rightsScope === "unknown") {
        setRightsReviewError("Select an explicit reuse scope before clearing rights.");
        return;
      }
      if (licenseIds.length === 0 || licenseIds.length !== licenseUrls.length) {
        setRightsReviewError("A cleared decision requires matching license identifiers and license URLs.");
        return;
      }
      if (!rightsAttribution.trim()) {
        setRightsReviewError("A cleared decision requires attribution text.");
        return;
      }
    }

    setRightsReviewBusy(true);
    setRightsReviewError("");
    setRightsReviewNotice("");
    try {
      const payload: ResearchSourceRightsReviewRequest = {
        decision: rightsDecision,
        scope: rightsScope,
        license_ids: licenseIds,
        license_urls: licenseUrls,
        rights_holder: rightsHolder.trim() || null,
        creators: splitValues(rightsCreators),
        source_revision_url: selectedResearchSource.rights_metadata.source_revision_url,
        attribution_text: rightsAttribution.trim() || null,
        reuse_restrictions: rightsRestrictions as ReuseRestriction[],
        review_notes: rightsReviewNotes.trim(),
        evidence_urls: evidenceUrls,
      };
      const result = await apiRequest<{
        review: ResearchSourceRightsReview;
        source: ResearchSource;
        requires_new_story_run: boolean;
      }>(
        `/organizations/${organizationId}/research-runs/${selectedResearchRunId}/sources/${selectedResearchSource.id}/rights-reviews`,
        { method: "POST", body: JSON.stringify(payload) },
      );
      setResearchSources((current) =>
        current.map((source) => source.id === result.source.id ? result.source : source),
      );
      setRightsReviewNotes("");
      setRightsEvidenceUrls("");
      setRightsReviewNotice(
        result.requires_new_story_run
          ? "Rights decision recorded. Existing story artifacts remain unchanged; start a new production run to regenerate and reverify the story."
          : "Rights decision recorded.",
      );
      await loadRightsReviews();
    } catch (err) {
      setRightsReviewError(
        err instanceof Error ? err.message : "Unable to record this rights decision.",
      );
    } finally {
      setRightsReviewBusy(false);
    }
  }

  async function approveProductionRun(allowEditorialWarnings = false) {
    if (!user || !organizationId || !selectedProductionRun) return;
    if (productionHardBlocker) {
      setProductionError(productionHardBlocker);
      return;
    }
    if (!publishTitle.trim()) {
      setProductionError("Enter a YouTube title before approval.");
      return;
    }
    if (productionEditorialWarnings.length && !allowEditorialWarnings) {
      setProductionError("Review the factual and source-rights warnings, or use the audited override below.");
      return;
    }
    if (allowEditorialWarnings && (!warningOverrideAcknowledged || warningOverrideReason.trim().length < 10)) {
      setProductionError("Confirm that you reviewed the warnings and enter a reason of at least 10 characters.");
      return;
    }
    const uploadChannel = channels.find((channel) => channel.id === selectedProductionRun.youtube_channel_id);
    if (!uploadChannel) {
      setProductionError("This run is not bound to an active connected YouTube channel. Create a new run for the channel you intend to publish on.");
      return;
    }
    if (!uploadChannel.can_publish) {
      setProductionError(`“${uploadChannel.title}” needs YouTube upload and video-update permissions. Reconnect it with publishing access before approving this run.`);
      return;
    }
    if (selectedProductionRun.youtube_channel_id !== productionChannelId) {
      const intendedChannel = channels.find((channel) => channel.id === productionChannelId);
      setProductionError(`This run is bound to “${uploadChannel.title}”, while the selected demo channel is “${intendedChannel?.title ?? "none"}”. Create a new production run for the selected channel before approving.`);
      return;
    }
    const warningNotice = allowEditorialWarnings
      ? `\n\nWARNING OVERRIDE: ${productionEditorialWarnings.length} factual/source-rights warning(s) will be recorded with your reason: “${warningOverrideReason.trim()}”.`
      : "";
    if (!window.confirm(`Approve “${publishTitle.trim()}” and immediately queue it to “${uploadChannel.title}” as ${publishPrivacy.toUpperCase()}?${warningNotice}`)) return;
    setProductionActionBusy(true);
    setProductionError("");
    clearMessages();
    try {
      await apiRequest<ProductionRun>(
        `/organizations/${organizationId}/production-runs/${selectedProductionRun.id}/approve`,
        {
          method: "POST",
          body: JSON.stringify({
            allow_editorial_warnings: allowEditorialWarnings,
            warning_override_reason: allowEditorialWarnings ? warningOverrideReason.trim() : null,
          }),
        },
      );
      const queued = await queueYouTubeUpload(selectedProductionRun.id);
      if (queued) {
        setNotice(`Approved and queued for YouTube as ${publishPrivacy}. Aurora will show upload and processing status here.`);
      } else {
        setNotice("Approval was recorded, but the upload could not be queued. Review the error and retry from YouTube publishing.");
      }
      await loadProductionRuns();
    } catch (err) {
      setProductionError(err instanceof Error ? err.message : "Unable to approve this production run.");
    } finally {
      setProductionActionBusy(false);
    }
  }

  async function rejectProductionRun() {
    if (!user || !organizationId || !selectedProductionRun) return;
    if (!window.confirm("Reject this video for publishing? The rendered video and its review history will be kept, and this run cannot be uploaded.")) return;
    setProductionActionBusy(true);
    setProductionError("");
    clearMessages();
    try {
      await apiRequest<ProductionRun>(
        `/organizations/${organizationId}/production-runs/${selectedProductionRun.id}/reject`,
        { method: "POST", body: JSON.stringify({ reason: "Rejected by channel editor." }) },
      );
      setPublishJob(null);
      setNotice("Video rejected. Its rendered files and review history remain available; it will not be uploaded.");
      await loadProductionRuns();
    } catch (err) {
      setProductionError(err instanceof Error ? err.message : "Unable to reject this video.");
    } finally {
      setProductionActionBusy(false);
    }
  }

  async function queueYouTubeUpload(productionRunId = selectedProductionRun?.id, retry = false): Promise<boolean> {
    if (!user || !organizationId || !productionRunId || publishRequestInFlight.current) return false;
    publishRequestInFlight.current = true;
    setPublishActionBusy(true);
    setPublishStatusError("");
    setProductionError("");
    clearMessages();
    setNotice(retry ? "Retry requested. Reconnecting to the YouTube publisher…" : "Starting YouTube upload. Aurora is creating a durable upload job…");
    try {
      const job = await apiRequest<{
        id: string;
        status: string;
        title: string;
        privacy_status: string;
        bytes_uploaded: number;
        byte_size: number;
        attempt_count: number;
        youtube_processing_status: string | null;
        youtube_video_id: string | null;
        failure_code: string | null;
        failure_message: string | null;
        thumbnail_upload_warning: string | null;
        published_at: string | null;
      }>(
        `/organizations/${organizationId}/production-runs/${productionRunId}/youtube-publish-jobs`,
        {
          method: "POST",
          headers: { "Idempotency-Key": retry ? `aurora-manual-retry:${productionRunId}:${Date.now()}` : `aurora-approved:${productionRunId}` },
          body: JSON.stringify({
            title: publishTitle.trim(),
            description: publishDescription.trim(),
            tags: [...new Set(publishTags.split(",").map((tag) => tag.trim()).filter(Boolean))],
            privacy_status: publishPrivacy,
          }),
        },
      );
      setPublishJob(job);
      setNotice("YouTube upload queued. This status card refreshes automatically while the worker uploads and YouTube processes the video.");
      void loadProductionPublishJob();
      await loadProductionRuns();
      return true;
    } catch (err) {
      setProductionError(err instanceof Error ? err.message : "Unable to queue the YouTube upload.");
      return false;
    } finally {
      publishRequestInFlight.current = false;
      setPublishActionBusy(false);
    }
  }

  async function reconcileYouTubeUpload() {
    if (!user || !organizationId || !publishJob) return;
    if (!window.confirm("YouTube already accepted this video. Resume reconciliation and continue without retrying the custom thumbnail?")) return;
    setPublishActionBusy(true);
    setProductionError("");
    clearMessages();
    try {
      const job = await apiRequest<typeof publishJob>(
        `/organizations/${organizationId}/youtube-publish-jobs/${publishJob.id}/reconcile`,
        { method: "POST" },
      );
      setPublishJob(job);
      setNotice("Existing YouTube video resumed. Aurora will verify processing and visibility without creating a duplicate upload.");
      await loadProductionRuns();
    } catch (err) {
      setProductionError(err instanceof Error ? err.message : "Unable to resume the YouTube video.");
    } finally {
      setPublishActionBusy(false);
    }
  }

  async function connectYouTube(
    purpose: "connect" | "publish" = "connect",
    channelId?: string,
  ) {
    if (!user || !organizationId) return;
    clearMessages();
    setLoading(true);
    try {
      const params = new URLSearchParams({ purpose });
      if (channelId) params.set("channel_record_id", channelId);
      const result = await apiRequest<{ authorization_url: string }>(
        `/organizations/${organizationId}/youtube/authorize?${params.toString()}`,
        { method: "POST" },
      );
      window.location.assign(result.authorization_url);
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Unable to start YouTube authorization.",
      );
      setLoading(false);
    }
  }

  async function selectPendingYouTubeChannel(channelId: string) {
    if (!user || !organizationId || !pendingSelectionId ||
        organizationId !== pendingSelectionOrganizationId) return;
    setPendingSelectionBusy(true);
    clearMessages();
    try {
      const channel = await apiRequest<YouTubeChannel>(
        `/organizations/${organizationId}/youtube-pending-selections/${pendingSelectionId}`,
        { method: "POST", body: JSON.stringify({ channel_id: channelId }) },
      );
      setChannels((current) => [channel, ...current.filter((item) => item.id !== channel.id)]);
      setPendingSelection(null);
      setPendingSelectionId("");
      setPendingSelectionOrganizationId("");
      setNotice(`"${channel.title}" is now connected to this workspace.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to connect the selected channel.");
    } finally {
      setPendingSelectionBusy(false);
    }
  }

  async function openChannelSettings(channel: YouTubeChannel) {
    if (!user || !organizationId) return;
    setSettingsChannel(channel);
    setChannelSettingsTab("voice");
    setChannelSettingsLoading(true);
    setEditorialProfile(null);
    setEditorialProfileVersion(null);
    setPublishingSettings(null);
    setPublishingDraft({ auto_publish_enabled: false, privacy_status: "private" });
    setScheduleDraft({
      enabled: false,
      timezone: "Asia/Kolkata",
      posting_time: "09:00",
      catch_up_policy: "skip",
    });
    clearMessages();
    const profilePath = `/organizations/${organizationId}/youtube-channels/${channel.id}/profile`;
    const schedulePath = `/organizations/${organizationId}/youtube-channels/${channel.id}/schedule`;
    const publishingPath = `/organizations/${organizationId}/youtube-channels/${channel.id}/publishing-settings`;
    const [profileResult, scheduleResult, publishingResult] = await Promise.allSettled([
      apiRequest<YouTubeChannelEditorialProfile>(profilePath),
      apiRequest<YouTubeChannelSchedule>(schedulePath),
      apiRequest<YouTubePublishingSettings>(publishingPath),
    ]);
    if (profileResult.status === "fulfilled") {
      const profile = profileResult.value.profile;
      const defaultVoice = defaultVoiceForProvider(profile.tts_provider);
      const allowedVoices: string[] = profile.tts_provider === "kokoro"
        ? kokoroVoiceChoices.map(([id]) => id)
        : profile.tts_provider === "qwen3_tts"
          ? qwenVoiceChoices.map(([id]) => id)
          : [];
      setEditorialProfile({
        ...profile,
        tts_voice: allowedVoices.length && !allowedVoices.includes(profile.tts_voice ?? "")
          ? defaultVoice
          : profile.tts_voice,
      });
      setEditorialProfileVersion(profileResult.value.version);
    } else if (profileResult.reason instanceof ApiError && profileResult.reason.status === 404) {
      setEditorialProfile({
        niche: "AI, GPU technology and robotics",
        topic_boundaries: ["GPU architecture", "AI research", "robotics", "data centers"],
        target_audience: "Curious general audience and technology learners",
        language: "en",
        tone: "Clear, engaging, evidence-led documentary",
        format: "educational",
        prohibited_topics: [],
        additional_constraints: [],
        visual_style: "Photorealistic editorial technology visuals with one clear subject per scene",
        visual_lighting: "cinematic",
        visual_motion: "gentle_drift",
        video_aspect_ratio: "9:16",
        caption_style: "bold_outline",
        caption_position: "lower_safe",
        caption_size: "medium",
      });
      setEditorialProfileVersion(null);
    } else {
      setError(profileResult.reason instanceof Error ? profileResult.reason.message : "Unable to load the editorial profile.");
    }
    if (scheduleResult.status === "fulfilled") {
      setScheduleDraft({
        enabled: scheduleResult.value.enabled,
        timezone: scheduleResult.value.timezone,
        posting_time: scheduleResult.value.posting_time.slice(0, 5),
        catch_up_policy: scheduleResult.value.catch_up_policy,
      });
    } else if (scheduleResult.reason instanceof ApiError && scheduleResult.reason.status === 404) {
      setScheduleDraft({
        enabled: false,
        timezone: "Asia/Kolkata",
        posting_time: "09:00",
        catch_up_policy: "skip",
      });
    } else {
      setError(scheduleResult.reason instanceof Error ? scheduleResult.reason.message : "Unable to load the daily schedule.");
    }
    if (publishingResult.status === "fulfilled") {
      setPublishingSettings(publishingResult.value);
      setPublishPrivacy(publishingResult.value.auto_publish_privacy_status);
      setPublishingDraft({
        auto_publish_enabled: publishingResult.value.auto_publish_enabled,
        privacy_status: publishingResult.value.auto_publish_privacy_status,
      });
    } else {
      setError(publishingResult.reason instanceof Error ? publishingResult.reason.message : "Unable to load automatic-publishing settings.");
    }
    setChannelSettingsLoading(false);
  }

  async function saveEditorialProfile(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!settingsChannel || !organizationId || !editorialProfile) return;
    setChannelSettingsSaving(true);
    clearMessages();
    try {
      const saved = await apiRequest<YouTubeChannelEditorialProfile>(
        `/organizations/${organizationId}/youtube-channels/${settingsChannel.id}/profile`,
        {
          method: "PUT",
          body: JSON.stringify({
            ...editorialProfile,
            tts_provider: editorialProfile.tts_provider || null,
            tts_voice: editorialProfile.tts_voice || null,
            tts_speed: editorialProfile.tts_speed ?? 1,
            tts_audio_format: editorialProfile.tts_audio_format ?? "wav",
          }),
        },
      );
      setEditorialProfile(saved.profile);
      setEditorialProfileVersion(saved.version);
      setNotice(`Editorial profile saved as version ${saved.version}. Existing production runs keep their original snapshots.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to save the editorial profile.");
    } finally {
      setChannelSettingsSaving(false);
    }
  }

  async function saveChannelSchedule(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!settingsChannel || !organizationId) return;
    setChannelSettingsSaving(true);
    clearMessages();
    try {
      const saved = await apiRequest<YouTubeChannelSchedule>(
        `/organizations/${organizationId}/youtube-channels/${settingsChannel.id}/schedule`,
        {
          method: "PUT",
          body: JSON.stringify({ ...scheduleDraft, posting_time: `${scheduleDraft.posting_time}:00` }),
        },
      );
      setScheduleDraft({
        enabled: saved.enabled,
        timezone: saved.timezone,
        posting_time: saved.posting_time.slice(0, 5),
        catch_up_policy: saved.catch_up_policy,
      });
      setNotice(saved.enabled
        ? `Daily schedule enabled. Next planned run: ${formatDate(saved.next_run_at ?? "")}.`
        : "Daily schedule paused.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to save the daily schedule.");
    } finally {
      setChannelSettingsSaving(false);
    }
  }

  async function savePublishingSettings(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!settingsChannel || !organizationId || !publishingSettings) return;
    setChannelSettingsSaving(true);
    clearMessages();
    try {
      const saved = await apiRequest<YouTubePublishingSettings>(
        `/organizations/${organizationId}/youtube-channels/${settingsChannel.id}/publishing-settings`,
        { method: "PUT", body: JSON.stringify(publishingDraft) },
      );
      setPublishingSettings(saved);
      setPublishingDraft({
        auto_publish_enabled: saved.auto_publish_enabled,
        privacy_status: saved.auto_publish_privacy_status,
      });
      setNotice(
        saved.auto_publish_enabled
          ? saved.automatic_execution_available
            ? `Automatic publishing enabled with ${saved.auto_publish_privacy_status} visibility after final QA.`
            : "Consent recorded. Automatic execution is not enabled in this deployment yet."
          : "Automatic publishing disabled. Uncompleted automatic jobs were cancelled.",
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to save automatic-publishing settings.");
    } finally {
      setChannelSettingsSaving(false);
    }
  }

  async function disconnectChannel(channel: YouTubeChannel) {
    if (!user || !organizationId) return;
    const confirmed = window.confirm(
      `Disconnect "${channel.title}" from this Aurora Studio workspace?`,
    );
    if (!confirmed) return;

    clearMessages();
    setBusyChannelId(channel.id);
    try {
      await apiRequest<void>(
        `/organizations/${organizationId}/youtube-channels/${channel.id}`,
        { method: "DELETE" },
      );
      setChannels((current) => current.filter((item) => item.id !== channel.id));
      setNotice(`"${channel.title}" has been disconnected.`);
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Unable to disconnect this channel.",
      );
    } finally {
      setBusyChannelId(null);
    }
  }

  async function signOut() {
    clearMessages();

    try {
      await logoutBrowserSession();
    } catch (err) {
      // Keep the authenticated UI intact: the server may still have a valid
      // session cookie if the logout request failed before revocation.
      setError(err instanceof Error ? err.message : "Unable to sign out.");
      return;
    }

    clearCsrfToken();
    channelRequestId.current += 1;
    setUser(null);
    setOrganizations([]);
    setOrganizationId("");
    setChannels([]);
    setPassword("");
    setWorkspaceLoading(false);
  }

  if (workspaceLoading) {
    return (
      <main className="loading-screen">
        <Brand />
        <p>Loading your workspace…</p>
      </main>
    );
  }

  if (!user) {
    return (
      <main className="auth-shell">
        <div className="auth-aside">
          <Brand />
          <div className="aside-copy">
            <span className="eyebrow">THE CREATOR WORKSPACE</span>
            <h1>Plan, make, and publish your videos.</h1>
            <p>Research topics, build evidence-backed stories, review each video, and manage your YouTube publishing workflow in one place.</p>
          </div>
          <div className="aside-foot">RESEARCH · CREATE · VERIFY · PUBLISH</div>
        </div>
        <section className="auth-panel">
          <div className="auth-card">
            <span className="eyebrow">AURORA STUDIO ACCOUNT</span>
            <h2>
              {authMode === "login" && "Welcome back"}
              {authMode === "register" && "Create your account"}
              {authMode === "verify" && "Verify your email"}
            </h2>
            <p className="muted">
              {authMode === "login" && "Sign in to manage your connected channels."}
              {authMode === "register" && "Start by creating your secure workspace account."}
              {authMode === "verify" && "Paste the verification token from your email."}
            </p>
            <Message notice={notice} error={error} />
            <form onSubmit={handleAuthSubmit} className="form-stack">
              {authMode !== "verify" && (
                <>
                  {authMode === "register" && (
                    <label>
                      Display name
                      <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} required minLength={1} maxLength={120} autoComplete="name" />
                    </label>
                  )}
                  <label>
                    Email address
                    <input type="email" value={authEmail} onChange={(e) => setAuthEmail(e.target.value)} required autoComplete="email" />
                  </label>
                  <label>
                    Password
                    <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required minLength={authMode === "register" ? 12 : 1} maxLength={128} autoComplete={authMode === "register" ? "new-password" : "current-password"} />
                    {authMode === "register" && <small>Use at least 12 characters.</small>}
                  </label>
                </>
              )}
              {authMode === "verify" && (
                <label>
                  Verification token
                  <textarea value={verificationToken} onChange={(e) => setVerificationToken(e.target.value)} required rows={3} />
                </label>
              )}
              <button className="button button-primary button-full" disabled={loading}>
                {loading ? "Please wait…" : authMode === "login" ? "Sign in" : authMode === "register" ? "Create account" : "Verify email"}
                <ArrowIcon />
              </button>
            </form>
            <div className="auth-switch">
              {authMode === "login" && <>New to Aurora Studio? <button className="text-button" onClick={() => { clearMessages(); setAuthMode("register"); }}>Create an account</button></>}
              {authMode === "register" && <>Already registered? <button className="text-button" onClick={() => { clearMessages(); setAuthMode("login"); }}>Sign in</button></>}
              {authMode === "verify" && <>Already verified? <button className="text-button" onClick={() => { clearMessages(); setAuthMode("login"); }}>Sign in</button></>}
            </div>
            {authMode === "verify" && <p className="helper">Email delivery depends on the backend email provider configuration. In local console mode, check the API logs for the verification link.</p>}
          </div>
        </section>
      </main>
    );
  }

  if (!user.email_verified) {
    return (
      <main className="loading-screen">
        <Brand />
        <div className="notice-card">
          <span className="status-dot status-warn" />
          <h2>Verify your email to continue</h2>
          <p className="muted">Aurora Studio requires a verified email address before workspace and channel access.</p>
          <button className="button button-primary" onClick={resendVerification} disabled={loading}>{loading ? "Sending…" : "Resend verification email"}</button>
          <button className="text-button" onClick={signOut}>Sign out</button>
        </div>
      </main>
    );
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <Brand />
        <div className="sidebar-label">WORKSPACE</div>
        <nav aria-label="Workspace" className="sidebar-navigation">
          {([
            ["overview", "◈", "Overview"],
            ["studio", "▶", "Video studio"],
            ["research", "⌕", "Research desk"],
            ["channels", "▣", "Channels"],
            ["settings", "⚙", "Settings"],
          ] as const).map(([page, glyph, label]) => (
            <button
              key={page}
              aria-label={label}
              aria-current={activePage === page ? "page" : undefined}
              className={`nav-item ${activePage === page ? "nav-item-active" : ""}`}
              onClick={() => setActivePage(page)}
              type="button"
            >
              <span aria-hidden="true" className="nav-glyph">{glyph}</span><span>{label}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="avatar">{user.display_name.slice(0, 1).toUpperCase()}</div>
          <div className="user-meta"><strong>{user.display_name}</strong><span>{user.email}</span></div>
          <button className="icon-button" onClick={signOut} title="Sign out" aria-label="Sign out"><LogoutIcon /></button>
        </div>
      </aside>

      <main className="main-content">
        <header className="topbar">
          <div className="breadcrumb">Workspace <span>/</span> {workspacePageCopy[activePage].title}</div>
          <div className="topbar-actions">
            <button className="theme-toggle" type="button" aria-pressed={theme === "dark"} onClick={() => setTheme((current) => current === "light" ? "dark" : "light")}>
              <span aria-hidden="true">{theme === "light" ? "☾" : "☀"}</span>{theme === "light" ? "Dark mode" : "Light mode"}
            </button>
            <div className="topbar-user">{user.display_name}<span className="online-dot" /></div>
          </div>
        </header>

        <section className={`page-wrap ${activePage === "research" ? "page-wrap-research" : ""}`}>
          <div className="page-heading">
            <div>
              <span className="eyebrow">{workspacePageCopy[activePage].eyebrow}</span>
              <h1>{workspacePageCopy[activePage].title}</h1>
              <p className="muted">{workspacePageCopy[activePage].description}</p>
            </div>
            <div className="heading-mark">A<span>·</span></div>
          </div>

          <Message notice={notice} error={error} />

          <section className={`workspace-bar ${activePage === "research" ? "workspace-bar-compact" : ""}`}>
            <div className="workspace-title"><span className="workspace-icon">⌂</span><div><strong>Active workspace</strong><small>{selectedOrganization?.slug ?? "Choose a workspace"}</small></div></div>
            <div className="workspace-actions">
              <select value={organizationId} onChange={(e) => setOrganizationId(e.target.value)} aria-label="Select workspace" disabled={organizations.length === 0}>
                {organizations.length === 0 && <option value="">No workspaces yet</option>}
                {organizations.map((org) => <option key={org.id} value={org.id}>{org.name}</option>)}
              </select>
              <button className="button button-secondary button-small" onClick={() => setShowCreateOrganization((value) => !value)}>+ New workspace</button>
            </div>
          </section>

          {showCreateOrganization && (
            <form className="create-workspace form-inline" onSubmit={createOrganization}>
              <label className="grow">Workspace name<input value={newOrganizationName} onChange={(e) => setNewOrganizationName(e.target.value)} required minLength={1} maxLength={160} placeholder="e.g. My Creator Studio" /></label>
              <button className="button button-secondary" type="button" onClick={() => { setShowCreateOrganization(false); setNewOrganizationName(""); }}>Cancel</button>
              <button className="button button-primary" disabled={loading}>{loading ? "Creating…" : "Create workspace"}</button>
            </form>
          )}

          {!selectedOrganization ? (
            <div className="empty-workspace">
              <div className="empty-icon">⌂</div>
              <h2>Create your first workspace</h2>
              <p className="muted">A workspace keeps your connected channels and future production activity organized.</p>
              <button className="button button-primary" onClick={() => setShowCreateOrganization(true)}>Create workspace <ArrowIcon /></button>
            </div>
          ) : (
            <>
              <div className="workspace-page" hidden={activePage !== "overview"}>
              <section className="research-panel readiness-panel" aria-labelledby="production-readiness-title">
                <div className="section-heading">
                  <div>
                    <h2 id="production-readiness-title">Production readiness</h2>
                    <p className="muted">See what is configured, then verify LLM and narration endpoint reachability. Connection checks do not generate content or publish videos.</p>
                  </div>
                  <div className="readiness-actions">
                    <button className="button button-secondary button-small" onClick={() => void loadProductionReadiness()} disabled={readinessLoading || readinessVerifying}>
                      {readinessLoading ? "Refreshing…" : "Refresh settings"}
                    </button>
                    <button className="button button-primary button-small" onClick={() => void verifyProductionReadiness()} disabled={readinessVerifying || readinessLoading}>
                      {readinessVerifying ? "Checking connections…" : "Verify connections"}
                    </button>
                  </div>
                </div>
                {readinessError ? <p className="readiness-error" role="alert">{readinessError}</p> : null}
                {productionReadiness ? (
                  <>
                    <div className={`readiness-summary readiness-${productionReadiness.summary}`}>
                      {productionReadiness.summary === "ready" ? "Ready to run" : productionReadiness.summary === "blocked" ? "Setup required" : "Needs attention"}
                    </div>
                    <details className="readiness-details" open={productionReadiness.checks.some((check) => check.key.endsWith("_connectivity"))}>
                      <summary>Review setup and connection details</summary>
                      <div className="readiness-grid">
                        {productionReadiness.checks.map((check) => (
                          <article className="readiness-check" key={check.key}>
                            <div className="readiness-check-heading"><strong>{check.title}</strong><span className={`readiness-status readiness-${check.status}`}>{check.status}</span></div>
                            <p>{check.detail}</p>
                            {check.action ? <small>{check.action}</small> : null}
                          </article>
                        ))}
                      </div>
                    </details>
                  </>
                ) : readinessLoading ? <p className="muted">Checking configured prerequisites…</p> : null}
              </section>
              <div className="overview-shortcuts" aria-label="Workspace shortcuts">
                <button type="button" onClick={() => setActivePage("studio")}>
                  <span>Video studio</span><strong>{productionRuns.length}</strong><small>Production runs</small>
                </button>
                <button type="button" onClick={() => setActivePage("research")}>
                  <span>Research desk</span><strong>{researchRuns.length}</strong><small>Research runs</small>
                </button>
                <button type="button" onClick={() => setActivePage("channels")}>
                  <span>Channels</span><strong>{channels.length}</strong><small>Connected channels</small>
                </button>
              </div>
              </div>

              <div className="workspace-page" hidden={activePage !== "channels"}>
              <div className="section-heading" id="channels-heading">
                <div><h2>Publishing channels</h2><p className="muted">Connect a YouTube channel to make it available to this workspace.</p></div>
                <div className="channel-page-actions">
                  <span className="count-pill">{channels.length} connected</span>
                  {channels.length > 0 && <button className="button button-primary button-small" onClick={() => void connectYouTube()} disabled={loading}>+ Add / reconnect</button>}
                </div>
              </div>

              {channels.length === 0 && (
              <section className="integration-card">
                <div className="integration-card-top">
                  <div className="service-brand"><YouTubeIcon /><div><strong>YouTube</strong><span>Video platform · Channel connection</span></div></div>
                  <span className="provider-badge"><span className="online-dot" /> GOOGLE OAUTH</span>
                </div>
                <div className="integration-body">
                  <div className="youtube-art"><YouTubeIcon /></div>
                  <div className="integration-copy">
                    <h3>Connect your YouTube channel</h3>
                    <p>Authorize Aurora Studio to view your channel and non-monetary analytics. Publishing permissions are not requested at this stage.</p>
                    <div className="scope-note"><LockIcon /> Read-only channel and analytics access · You can revoke access at any time</div>
                  </div>
                  <button className="button button-primary connect-button" onClick={() => void connectYouTube()} disabled={loading}>
                    {loading ? "Connecting…" : "Connect YouTube"} <ArrowIcon />
                  </button>
                </div>
              </section>
              )}

              {pendingSelectionId && pendingSelectionOrganizationId === organizationId && (
                <section className="research-panel" aria-labelledby="pending-youtube-selection">
                  <div className="section-heading">
                    <div>
                      <h2 id="pending-youtube-selection">Choose the channel to connect</h2>
                      <p className="muted">Google authorized this account. No channel has been connected yet. Select the exact channel you want in this workspace.</p>
                    </div>
                  </div>
                  {pendingSelectionLoading ? (
                    <div className="channel-loading"><span className="spinner" /> Loading available channels…</div>
                  ) : pendingSelection ? (
                    <div className="channel-list">
                      {pendingSelection.channels.map((choice) => (
                        <article className="channel-row" key={choice.channel_id}>
                          {choice.thumbnail_url ? <img className="channel-avatar" src={choice.thumbnail_url} alt="" referrerPolicy="no-referrer" /> : <div className="channel-avatar channel-avatar-fallback"><YouTubeIcon /></div>}
                          <div className="channel-details"><strong>{choice.title}</strong><span>{choice.custom_url || choice.channel_id}</span></div>
                          <button className="button button-primary button-small" onClick={() => void selectPendingYouTubeChannel(choice.channel_id)} disabled={pendingSelectionBusy}>
                            {pendingSelectionBusy ? "Connecting…" : "Connect this channel"}
                          </button>
                        </article>
                      ))}
                    </div>
                  ) : (
                    <p className="muted">These channel choices are no longer available. Start a new connection to try again.</p>
                  )}
                </section>
              )}

              <div className="section-heading channel-heading">
                <div><h2>Connected channels</h2><p className="muted">Channels linked to {selectedOrganization.name}.</p></div>
                <button className="text-button refresh-button" onClick={() => void loadChannels()} disabled={channelsLoading}>{channelsLoading ? "Refreshing…" : "↻ Refresh"}</button>
              </div>

              {channelsLoading ? (
                <div className="channel-loading"><span className="spinner" /> Loading connected channels…</div>
              ) : channels.length === 0 ? (
                <div className="empty-channels">
                  <div className="empty-icon"><YouTubeIcon /></div>
                  <h3>No channels connected yet</h3>
                  <p className="muted">Your connected YouTube channels will appear here.</p>
                </div>
              ) : (
                <div className="channel-list channel-grid">
                  {channels.map((channel) => (
                    <article className="channel-row channel-card" key={channel.id}>
                      {channel.thumbnail_url ? <img className="channel-avatar" src={channel.thumbnail_url} alt="" referrerPolicy="no-referrer" /> : <div className="channel-avatar channel-avatar-fallback"><YouTubeIcon /></div>}
                      <div className="channel-details"><strong>{channel.title}</strong><span>{channel.custom_url || channel.channel_id}</span><small>Connected {formatDate(channel.connected_at)}</small></div>
                      <span className="connected-pill"><span className="online-dot" /> Connected</span>
                      <span className={`scope-status-pill ${channel.can_publish ? "scope-status-enabled" : "scope-status-readonly"}`}>
                        <LockIcon /> {channel.can_publish
                          ? "Publishing enabled"
                          : channel.granted_scopes.includes(YOUTUBE_UPLOAD_SCOPE)
                            ? "Video update permission missing"
                            : "Read-only"}
                      </span>
                      <div className="channel-card-actions">
                      <button
                        className="button button-secondary button-small"
                        onClick={() => void loadChannelAnalytics(channel)}
                        disabled={!channel.granted_scopes.includes(YOUTUBE_ANALYTICS_SCOPE) || analyticsLoading}
                        title={!channel.granted_scopes.includes(YOUTUBE_ANALYTICS_SCOPE) ? "Reconnect to grant YouTube Analytics read-only access" : undefined}
                      >
                        {analyticsLoading && analyticsChannel?.id === channel.id ? "Loading…" : "Analytics"}
                      </button>
                      {!channel.granted_scopes.includes(YOUTUBE_ANALYTICS_SCOPE) && (
                        <button className="button button-secondary button-small" onClick={() => void connectYouTube()} disabled={loading}>
                          {loading ? "Opening Google…" : "Reconnect for analytics"}
                        </button>
                      )}
                      {!channel.can_publish && (
                        <button className="button button-secondary button-small" onClick={() => void connectYouTube("publish", channel.id)} disabled={loading}>
                          {loading ? "Opening consent…" : channel.granted_scopes.includes(YOUTUBE_UPLOAD_SCOPE) ? "Reconnect for video updates" : "Enable publishing"}
                        </button>
                      )}
                      <button className="button button-secondary button-small" onClick={() => void openChannelSettings(channel)} disabled={channelSettingsLoading}>Profile & schedule</button>
                      <button className="button button-danger-ghost" onClick={() => void disconnectChannel(channel)} disabled={busyChannelId === channel.id}>{busyChannelId === channel.id ? "Disconnecting…" : "Disconnect"}</button>
                      </div>
                    </article>
                  ))}
                </div>
              )}
              {analyticsChannel && (
                <section className="research-panel analytics-panel" aria-labelledby="analytics-title" aria-live="polite">
                  <div className="section-heading">
                    <div>
                      <h2 id="analytics-title">{analyticsChannel.title} · Channel analytics</h2>
                      <p className="muted">Verified YouTube Analytics for the last 28 calendar days, ending yesterday. Recent days can take 48–72 hours to appear.</p>
                    </div>
                    <div className="analytics-actions">
                      <button className="button button-secondary button-small" onClick={() => void loadChannelAnalytics(analyticsChannel)} disabled={analyticsLoading}>{analyticsLoading ? "Refreshing…" : "Refresh"}</button>
                      <button className="text-button" onClick={() => { setAnalyticsChannel(null); setAnalyticsData(null); setAnalyticsError(""); }}>Close</button>
                    </div>
                  </div>
                  {analyticsError ? <p className="readiness-error" role="alert">{analyticsError}</p> : null}
                  {analyticsLoading && !analyticsData ? <div className="channel-loading"><span className="spinner" /> Loading verified channel metrics…</div> : null}
                  {analyticsData && analyticsData.snapshot_count > 0 ? <p className="analytics-snapshot-note">Saved {analyticsData.snapshot_count} daily snapshots for future comparisons · {analyticsData.snapshot_saved_at ? new Date(analyticsData.snapshot_saved_at).toLocaleString() : "just now"}</p> : null}
                  {analyticsData && analyticsData.pending_days > 0 ? <p className="analytics-disclaimer" role="status">Requested through {analyticsData.end_date}. YouTube’s latest reported day is {analyticsData.latest_report_day ?? "not available"}; {analyticsData.pending_days} day{analyticsData.pending_days === 1 ? " is" : "s are"} still pending. Pending reporting is not counted as zero.</p> : null}
                  {analyticsData && analyticsData.rows.length === 0 ? <div className="research-empty">YouTube returned no analytics for this period. This is unavailable data, not a zero-view result.</div> : null}
                  {analyticsData?.comparison ? (
                    <section className="analytics-insight" aria-labelledby="analytics-insight-title">
                      <div className="analytics-insight-heading">
                        <div>
                          <h3 id="analytics-insight-title">Seven-day comparison</h3>
                          <p className="muted">{analyticsData.comparison.current_start_date}–{analyticsData.comparison.current_end_date} compared with {analyticsData.comparison.previous_start_date}–{analyticsData.comparison.previous_end_date}</p>
                        </div>
                        <span className="analytics-review-badge">Review cue · not applied</span>
                      </div>
                      <div className="analytics-comparison-values">
                        <div><span>Views</span><strong>{analyticsData.comparison.current_views.toLocaleString()}</strong><small>{analyticsData.comparison.views_change_percent === null ? "Previous period had no views" : `${analyticsData.comparison.views_change_percent > 0 ? "+" : ""}${analyticsData.comparison.views_change_percent}% vs previous week`}</small></div>
                        <div><span>Watch minutes</span><strong>{analyticsData.comparison.current_watch_minutes.toLocaleString()}</strong><small>{analyticsData.comparison.watch_minutes_change_percent === null ? "Previous period had no watch time" : `${analyticsData.comparison.watch_minutes_change_percent > 0 ? "+" : ""}${analyticsData.comparison.watch_minutes_change_percent}% vs previous week`}</small></div>
                        <div><span>Average viewed</span><strong>{analyticsData.comparison.current_average_view_percentage}%</strong><small>{analyticsData.comparison.average_view_percentage_change_points > 0 ? "+" : ""}{analyticsData.comparison.average_view_percentage_change_points} points</small></div>
                      </div>
                      <p className="analytics-suggestion"><strong>Consider:</strong> {analyticsData.comparison.suggestion}</p>
                      <p className="analytics-disclaimer">This describes reported metrics but does not establish cause. Only approved cues are sent as optional creative context; the generated story still goes through normal review.</p>
                      <div className="analytics-review-controls">
                        <span className={`analytics-review-status analytics-review-${analyticsData.comparison.review.status}`}>{analyticsData.comparison.review.status === "approved" ? "Approved for future story context" : analyticsData.comparison.review.status === "dismissed" ? "Dismissed" : "Awaiting review"}</span>
                        {analyticsData.comparison.review.status !== "approved" ? <button className="button button-primary button-small" onClick={() => void decideAnalyticsReview("approved")} disabled={analyticsReviewBusy}>{analyticsReviewBusy ? "Saving…" : "Approve for future stories"}</button> : null}
                        {analyticsData.comparison.review.status !== "dismissed" ? <button className="button button-secondary button-small" onClick={() => void decideAnalyticsReview("dismissed")} disabled={analyticsReviewBusy}>{analyticsReviewBusy ? "Saving…" : "Dismiss cue"}</button> : null}
                      </div>
                    </section>
                  ) : analyticsData ? (
                    <p className="analytics-comparison-empty">Trend review is unavailable because YouTube did not return every day for two complete weeks. Missing report days are not counted as zero.</p>
                  ) : null}
                  {analyticsData && (() => {
                    const reportedByDay = new Map(analyticsData.rows.map((row) => [row.day, row]));
                    const calendarRows: Array<{
                      day: string;
                      row: YouTubeAnalytics["rows"][number] | null;
                      state: "reported" | "pending" | "unavailable";
                    }> = [];
                    const cursor = new Date(`${analyticsData.start_date}T00:00:00Z`);
                    const finalDay = new Date(`${analyticsData.end_date}T00:00:00Z`);
                    while (cursor <= finalDay) {
                      const day = cursor.toISOString().slice(0, 10);
                      const row = reportedByDay.get(day) ?? null;
                      calendarRows.push({
                        day,
                        row,
                        state: row
                          ? "reported"
                          : analyticsData.latest_report_day && day > analyticsData.latest_report_day
                            ? "pending"
                            : "unavailable",
                      });
                      cursor.setUTCDate(cursor.getUTCDate() + 1);
                    }
                    const totals = analyticsData.rows.reduce((acc, row) => ({
                      views: acc.views + row.views,
                      watch: acc.watch + row.minutes_watched,
                      gained: acc.gained + row.subscribers_gained,
                      lost: acc.lost + row.subscribers_lost,
                      likes: acc.likes + row.likes,
                      comments: acc.comments + row.comments,
                      shares: acc.shares + row.shares,
                    }), { views: 0, watch: 0, gained: 0, lost: 0, likes: 0, comments: 0, shares: 0 });
                    return <>
                      {analyticsData.rows.length > 0 && <div className="analytics-summary">
                        <div><span>Reported views</span><strong>{totals.views.toLocaleString()}</strong></div>
                        <div><span>Reported watch time</span><strong>{totals.watch.toLocaleString(undefined, { maximumFractionDigits: 1 })} min</strong></div>
                        <div><span>Net subscribers</span><strong>{(totals.gained - totals.lost).toLocaleString()}</strong></div>
                        <div><span>Reported engagements</span><strong>{(totals.likes + totals.comments + totals.shares).toLocaleString()}</strong></div>
                      </div>}
                      <div className="analytics-table-wrap">
                        <table className="analytics-table">
                          <thead><tr><th scope="col">Day</th><th scope="col">Views</th><th scope="col">Watch minutes</th><th scope="col">Subscribers</th><th scope="col">Likes</th><th scope="col">Comments</th><th scope="col">Shares</th></tr></thead>
                          <tbody>{calendarRows.slice().reverse().map(({ day, row, state }) => <tr key={day}>
                            <th scope="row">{day}</th>
                            {row ? <>
                              <td>{row.views.toLocaleString()}</td><td>{row.minutes_watched.toLocaleString(undefined, { maximumFractionDigits: 1 })}</td><td>+{row.subscribers_gained} / −{row.subscribers_lost}</td><td>{row.likes}</td><td>{row.comments}</td><td>{row.shares}</td>
                            </> : <td className={`analytics-no-data analytics-no-data-${state}`} colSpan={6}>
                              {state === "pending" ? "Awaiting YouTube report · not counted as zero" : "No daily row returned · value unavailable"}
                            </td>}
                          </tr>)}</tbody>
                        </table>
                      </div>
                    </>;
                  })()}
                </section>
              )}
              {settingsChannel && (
                <section className="research-panel" aria-labelledby="channel-settings-title">
                  <div className="section-heading">
                    <div>
                      <h2 id="channel-settings-title">{settingsChannel.title} · Channel settings</h2>
                      <p className="muted">Editorial profile versions are immutable. Each new production run snapshots the latest version.</p>
                    </div>
                    <button type="button" className="text-button" onClick={() => setSettingsChannel(null)}>Close</button>
                  </div>
                  {channelSettingsLoading ? (
                    <div className="channel-loading"><span className="spinner" /> Loading channel settings…</div>
                  ) : editorialProfile ? (
                    <>
                      <nav className="channel-settings-tabs" role="tablist" aria-label="Channel settings sections">
                        {([
                          ["voice", "Voice & audio"],
                          ["visual", "Visuals"],
                          ["captions", "Captions"],
                          ["audience", "Audience"],
                          ["safety", "Content rules"],
                          ["publishing", "Publishing"],
                        ] as const).map(([tab, label]) => (
                          <button
                            key={tab}
                            type="button"
                            role="tab"
                            aria-selected={channelSettingsTab === tab}
                            className={`channel-settings-tab${channelSettingsTab === tab ? " is-active" : ""}`}
                            onClick={() => setChannelSettingsTab(tab)}
                          >{label}</button>
                        ))}
                      </nav>
                      <form className="form-stack" onSubmit={saveEditorialProfile} noValidate>
                        <div className="research-detail-heading">
                          <h3>Editorial profile {editorialProfileVersion ? `· v${editorialProfileVersion}` : "· Not configured"}</h3>
                        </div>
                        <section hidden={channelSettingsTab !== "voice"} className="provider-settings-card" aria-labelledby="narration-settings-title">
                          <div className="provider-settings-heading">
                            <div>
                              <h4 id="narration-settings-title">Narration for this channel</h4>
                              <p>Choose a voice setup for new videos. Provider URLs, model downloads, and credentials stay in your local runtime configuration.</p>
                            </div>
                            <span className="provider-settings-tag">VIDEO AUDIO</span>
                          </div>
                          <div className="form-inline">
                            <label className="grow">Speech provider
                              <select
                                value={editorialProfile.tts_provider ?? "environment"}
                                onChange={(event) => setEditorialProfile((current) => current ? {
                                  ...current,
                                  tts_provider: event.target.value === "environment"
                                    ? null
                                    : event.target.value as NonNullable<typeof current.tts_provider>,
                                  tts_voice: defaultVoiceForProvider(event.target.value),
                                  tts_speed: event.target.value === "qwen3_tts"
                                    ? 1
                                    : event.target.value === "elevenlabs"
                                      ? Math.min(current.tts_speed ?? 1, 1.2)
                                      : current.tts_speed ?? 1,
                                } : current)}
                              >
                                <option value="environment">Use the configured default ({productionReadiness?.tts_provider ?? "loading"})</option>
                                <option value="qwen3_tts" disabled={productionReadiness?.tts_provider !== "qwen3_tts"}>Qwen3-TTS local bridge{productionReadiness?.tts_provider === "qwen3_tts" ? " · configured" : " · not configured"}</option>
                                <option value="kokoro" disabled={productionReadiness?.tts_provider !== "kokoro"}>Kokoro-compatible local service{productionReadiness?.tts_provider === "kokoro" ? " · configured" : " · not configured"}</option>
                                <option value="openai_compatible" disabled={productionReadiness?.tts_provider !== "openai_compatible"}>Other OpenAI-compatible service{productionReadiness?.tts_provider === "openai_compatible" ? " · configured" : " · not configured"}</option>
                                <option value="elevenlabs" disabled={productionReadiness?.tts_provider !== "elevenlabs"}>ElevenLabs{productionReadiness?.tts_provider === "elevenlabs" ? " · configured" : " · not configured"}</option>
                              </select>
                              <small>Choose a provider configured in this local runtime. To switch providers, update its endpoint and credentials in `.env`, then restart Aurora.</small>
                            </label>
                            <label className="grow">Voice
                              {editorialProfile.tts_provider === "kokoro" ? (
                                <select value={editorialProfile.tts_voice ?? "af_heart"} onChange={(event) => setEditorialProfile((current) => current ? { ...current, tts_voice: event.target.value } : current)}>
                                  {kokoroVoiceChoices.map(([id, label]) => <option key={id} value={id} disabled={Boolean(productionReadiness?.tts_voice_options.length) && !productionReadiness?.tts_voice_options.includes(id)}>{label}{productionReadiness?.tts_voice_options.length && !productionReadiness.tts_voice_options.includes(id) ? " · not enabled here" : ""}</option>)}
                                </select>
                              ) : editorialProfile.tts_provider === "qwen3_tts" ? (
                                <select value={editorialProfile.tts_voice ?? "Ryan"} onChange={(event) => setEditorialProfile((current) => current ? { ...current, tts_voice: event.target.value } : current)}>
                                  {qwenVoiceChoices.map(([id, label]) => <option key={id} value={id} disabled={Boolean(productionReadiness?.tts_voice_options.length) && !productionReadiness?.tts_voice_options.includes(id)}>{label}{productionReadiness?.tts_voice_options.length && !productionReadiness.tts_voice_options.includes(id) ? " · not enabled here" : ""}</option>)}
                                </select>
                              ) : (
                                <input
                                  value={editorialProfile.tts_voice ?? ""}
                                  onChange={(event) => setEditorialProfile((current) => current ? { ...current, tts_voice: event.target.value || null } : current)}
                                  maxLength={100}
                                  placeholder="Use provider default"
                                />
                              )}
                              <small>{editorialProfile.tts_provider === "kokoro" ? `Kokoro voice IDs are shown here; this runtime exposes ${productionReadiness?.tts_voice_options.join(", ") || "provider-defined voices"}.` : editorialProfile.tts_provider === "qwen3_tts" ? `Qwen3-TTS voices are shown here; this runtime exposes ${productionReadiness?.tts_voice_options.join(", ") || "provider-defined voices"}.` : "External providers may use their own voice IDs."}</small>
                            </label>
                            <label>Speech speed
                              <select
                                value={editorialProfile.tts_speed ?? 1}
                                disabled={editorialProfile.tts_provider === "qwen3_tts"}
                                onChange={(event) => setEditorialProfile((current) => current ? { ...current, tts_speed: Number(event.target.value) } : current)}
                              >
                                <option value="0.9">0.9× · relaxed</option>
                                <option value="1">1.0× · natural</option>
                                <option value="1.1">1.1× · brisk</option>
                                {editorialProfile.tts_provider !== "elevenlabs" && <option value="1.2">1.2× · faster</option>}
                              </select>
                            </label>
                          </div>
                          {editorialProfile.tts_provider === "qwen3_tts" && <p className="provider-settings-note">Qwen3-TTS bridge is set to English and natural speed (1.0×). Change those defaults in the bridge configuration if needed.</p>}
                          <p className="provider-settings-note"><strong>Setup stays on this machine.</strong> Set <code>MEDIA_TTS_BASE_URL</code>, model, rights, and secret key in your local runtime configuration; this channel profile stores only the provider and voice preference.</p>
                        </section>
                        <section hidden={channelSettingsTab !== "captions"} className="provider-settings-card" aria-labelledby="caption-settings-title">
                          <div className="provider-settings-heading">
                            <div>
                              <h4 id="caption-settings-title">Caption appearance</h4>
                              <p>These defaults apply to new videos for this channel.</p>
                            </div>
                            <span className="provider-settings-tag">VIDEO TEXT</span>
                          </div>
                          <div className="form-inline">
                            <label className="grow">Caption look<select value={editorialProfile.caption_style ?? "bold_outline"} onChange={(event) => setEditorialProfile((current) => current ? { ...current, caption_style: event.target.value as "classic" | "bold_outline" } : current)}>
                              <option value="bold_outline">Bold white with dark outline</option>
                              <option value="classic">White text on dark panel</option>
                            </select><small>Captions are burned into the video and also provided as a subtitle file.</small></label>
                            <label className="grow">Placement<select value={editorialProfile.caption_position ?? "lower_safe"} onChange={(event) => setEditorialProfile((current) => current ? { ...current, caption_position: event.target.value as "lower_safe" | "center" } : current)}>
                              <option value="lower_safe">Lower safe area</option>
                              <option value="center">Center</option>
                            </select><small>Lower safe area avoids most Shorts controls.</small></label>
                            <label className="grow">Text size<select value={editorialProfile.caption_size ?? "medium"} onChange={(event) => setEditorialProfile((current) => current ? { ...current, caption_size: event.target.value as "small" | "medium" | "large" } : current)}>
                              <option value="small">Small</option>
                              <option value="medium">Medium · recommended</option>
                              <option value="large">Large</option>
                            </select></label>
                          </div>
                          <p className="provider-settings-note">Word-by-word yellow highlighting needs a word-alignment service. The current local Kokoro voice does not provide word timestamps, so Aurora keeps captions phrase-synced rather than displaying inaccurate karaoke timing.</p>
                        </section>
                        <section hidden={channelSettingsTab !== "visual"} className="provider-settings-card" aria-labelledby="visual-settings-title">
                          <div className="provider-settings-heading">
                            <div>
                              <h4 id="visual-settings-title">Visual direction</h4>
                              <p>These versioned defaults guide generated scene art and the final FFmpeg render.</p>
                            </div>
                            <span className="provider-settings-tag">SCENES & VIDEO</span>
                          </div>
                          <label>Visual style prompt<textarea rows={2} maxLength={500} value={editorialProfile.visual_style ?? ""} onChange={(event) => setEditorialProfile((current) => current ? { ...current, visual_style: event.target.value } : current)} placeholder="e.g. Photorealistic editorial documentary, restrained palette, crisp subject separation" /><small>ComfyUI uses this direction alongside each scene prompt. Avoid requesting text in generated imagery.</small></label>
                          <div className="form-inline">
                            <label className="grow">Lighting<select value={editorialProfile.visual_lighting ?? "cinematic"} onChange={(event) => setEditorialProfile((current) => current ? { ...current, visual_lighting: event.target.value as NonNullable<typeof current.visual_lighting> } : current)}>
                              <option value="natural">Natural daylight</option><option value="cinematic">Cinematic</option><option value="studio">Studio softbox</option><option value="dramatic">Dramatic contrast</option>
                            </select></label>
                            <label className="grow">Scene motion<select value={editorialProfile.visual_motion ?? "gentle_drift"} onChange={(event) => setEditorialProfile((current) => current ? { ...current, visual_motion: event.target.value as NonNullable<typeof current.visual_motion> } : current)}>
                              <option value="gentle_drift">Gentle camera drift</option><option value="static">Static frame</option>
                            </select></label>
                            <label className="grow">Video shape<select value={editorialProfile.video_aspect_ratio ?? "9:16"} onChange={(event) => setEditorialProfile((current) => current ? { ...current, video_aspect_ratio: event.target.value as NonNullable<typeof current.video_aspect_ratio> } : current)}>
                              <option value="9:16">Vertical · Shorts (9:16)</option><option value="16:9">Landscape (16:9)</option><option value="1:1">Square (1:1)</option>
                            </select></label>
                          </div>
                        </section>
                        <div hidden={channelSettingsTab !== "audience"} className="form-inline">
                          <label className="grow">Channel niche
                            <select value={nichePresets.includes(editorialProfile.niche as typeof nichePresets[number]) ? editorialProfile.niche : "custom"} onChange={(event) => setEditorialProfile((current) => current ? { ...current, niche: event.target.value === "custom" ? "" : event.target.value } : current)}>
                              {nichePresets.filter((item) => item !== "Custom niche…").map((item) => <option key={item} value={item}>{item}</option>)}
                              <option value="custom">Custom niche…</option>
                            </select>
                            {(!nichePresets.includes(editorialProfile.niche as typeof nichePresets[number])) && <input value={editorialProfile.niche} onChange={(event) => setEditorialProfile((current) => current ? { ...current, niche: event.target.value } : current)} required maxLength={120} placeholder="Describe your channel niche" />}
                            <small>Save once. The daily schedule will rotate through fresh topics within this niche and your topic boundaries.</small>
                          </label>
                          <label className="grow">Target audience<input value={editorialProfile.target_audience} onChange={(event) => setEditorialProfile((current) => current ? { ...current, target_audience: event.target.value } : current)} required maxLength={300} placeholder="e.g. curious technology professionals" /></label>
                        </div>
                        <div hidden={channelSettingsTab !== "audience"} className="form-inline">
                          <label className="grow">Language<input value={editorialProfile.language} onChange={(event) => setEditorialProfile((current) => current ? { ...current, language: event.target.value } : current)} required maxLength={80} /></label>
                          <label className="grow">Tone<input value={editorialProfile.tone} onChange={(event) => setEditorialProfile((current) => current ? { ...current, tone: event.target.value } : current)} required maxLength={80} /></label>
                          <label className="grow">Format<select value={editorialProfile.format} onChange={(event) => setEditorialProfile((current) => current ? { ...current, format: event.target.value as typeof current.format } : current)}>
                            <option value="educational">Educational</option>
                            <option value="documentary">Documentary</option>
                            <option value="news">News</option>
                            <option value="storytelling">Storytelling</option>
                            <option value="commentary">Commentary</option>
                            <option value="entertainment">Entertainment</option>
                          </select></label>
                        </div>
                        <div hidden={channelSettingsTab !== "safety"} className="form-stack channel-content-rules">
                          <label>Topic boundaries (one per line)<textarea rows={3} value={editorialProfile.topic_boundaries.join("\n")} onChange={(event) => setEditorialProfile((current) => current ? { ...current, topic_boundaries: event.target.value.split("\n").map((value) => value.trim()).filter(Boolean) } : current)} /></label>
                          <label>Prohibited topics (one per line)<textarea rows={3} value={editorialProfile.prohibited_topics.join("\n")} onChange={(event) => setEditorialProfile((current) => current ? { ...current, prohibited_topics: event.target.value.split("\n").map((value) => value.trim()).filter(Boolean) } : current)} /></label>
                          <label>Additional constraints (one per line)<textarea rows={3} value={editorialProfile.additional_constraints.join("\n")} onChange={(event) => setEditorialProfile((current) => current ? { ...current, additional_constraints: event.target.value.split("\n").map((value) => value.trim()).filter(Boolean) } : current)} /></label>
                        </div>
                        {channelSettingsTab !== "publishing" && <button className="button button-primary" disabled={channelSettingsSaving}>{channelSettingsSaving ? "Saving…" : "Save new profile version"}</button>}
                      </form>
                      <div hidden={channelSettingsTab !== "publishing"}>
                      {publishingSettings && (
                        <>
                          <form className="form-stack" onSubmit={savePublishingSettings}>
                            <h3>Automatic publishing</h3>
                            <p className="research-helper">
                              The visibility below is the default for manual uploads too. Automatic publishing stays off unless you separately enable channel-owner consent; any automatic run must pass factual, rights and technical QA. Revoking consent stops uncompleted automatic jobs.
                            </p>
                            <label className="checkbox-row">
                              <input
                                type="checkbox"
                                checked={publishingDraft.auto_publish_enabled}
                                disabled={!publishingSettings.can_manage_auto_publish && !publishingSettings.auto_publish_enabled}
                                onChange={(event) => setPublishingDraft((current) => ({ ...current, auto_publish_enabled: event.target.checked }))}
                              />
                              Allow Aurora to publish completed videos automatically
                            </label>
                            <label>
                              Default visibility after processing
                              <select
                                value={publishingDraft.privacy_status}
                                disabled={!publishingSettings.can_manage_auto_publish}
                                onChange={(event) => setPublishingDraft((current) => ({ ...current, privacy_status: event.target.value as "private" | "unlisted" | "public" }))}
                              >
                                <option value="private">Private</option>
                                <option value="unlisted">Unlisted</option>
                                <option value="public">Public</option>
                              </select>
                            </label>
                            {!publishingSettings.can_manage_auto_publish && (
                              <p className="research-helper">Only the account that connected this YouTube channel can enable automation or change its visibility policy. A workspace administrator can revoke existing consent.</p>
                            )}
                            {!publishingSettings.automatic_execution_available && (
                              <p className="research-helper">The publisher worker is not enabled in this deployment. Consent can be configured, but automatic production-to-publishing handoff will remain paused until the worker is enabled.</p>
                            )}
                            {publishingSettings.consent_granted_at && (
                              <p className="research-helper">Consent last granted {formatDate(publishingSettings.consent_granted_at)}.</p>
                            )}
                            <button
                              className="button button-secondary"
                              disabled={channelSettingsSaving || (!publishingSettings.can_manage_auto_publish && !(publishingSettings.auto_publish_enabled && !publishingDraft.auto_publish_enabled))}
                            >
                              {channelSettingsSaving ? "Saving…" : "Save publishing policy"}
                            </button>
                          </form>
                          <div className="channel-settings-divider" />
                        </>
                      )}
                      <form className="form-stack" onSubmit={saveChannelSchedule}>
                        <h3>Daily publishing schedule</h3>
                        <label className="checkbox-row"><input type="checkbox" checked={scheduleDraft.enabled} onChange={(event) => setScheduleDraft((current) => ({ ...current, enabled: event.target.checked }))} /> Enable daily schedule</label>
                        <div className="form-inline">
                          <label className="grow">Timezone (IANA)<input value={scheduleDraft.timezone} onChange={(event) => setScheduleDraft((current) => ({ ...current, timezone: event.target.value }))} required maxLength={64} placeholder="Asia/Kolkata" /></label>
                          <label>Posting time<input type="time" value={scheduleDraft.posting_time} onChange={(event) => setScheduleDraft((current) => ({ ...current, posting_time: event.target.value }))} required /></label>
                          <label>Missed-run policy<select value={scheduleDraft.catch_up_policy} onChange={(event) => setScheduleDraft((current) => ({ ...current, catch_up_policy: event.target.value as "skip" | "run_once" }))}><option value="skip">Skip missed days</option><option value="run_once">Catch up once</option></select></label>
                        </div>
                        <button className="button button-secondary" disabled={channelSettingsSaving}>{channelSettingsSaving ? "Saving…" : "Save daily schedule"}</button>
                        <p className="research-helper">Daily schedules queue research and production automatically. Without automatic-publishing consent, each completed production run waits for human review. With consent and an enabled publisher worker, the final QA gate can create an auditable automatic publishing job.</p>
                      </form>
                      </div>
                    </>
                  ) : (
                    <p className="muted">Unable to load the channel editorial profile.</p>
                  )}
                </section>
              )}
              <p className="permission-note"><LockIcon /> Only workspace owners and admins can connect or disconnect channels. If an action is denied, ask a workspace owner or admin to update your access.</p>
              </div>

              <div className="workspace-page" hidden={activePage !== "settings"}>
                <section className="settings-intro">
                  <div>
                    <span className="settings-kicker">LOCAL RUNTIME</span>
                    <h2>Generation and publishing setup</h2>
                    <p>Choose what Aurora can run in this workspace. Provider keys stay in the local <code>.env</code> file and are never shown here.</p>
                  </div>
                  <div className="settings-actions">
                    <button className="button button-secondary button-small" onClick={() => void loadProductionReadiness()} disabled={readinessLoading || readinessVerifying}>{readinessLoading ? "Refreshing…" : "Refresh status"}</button>
                    <button className="button button-primary button-small" onClick={() => void verifyProductionReadiness()} disabled={readinessVerifying || readinessLoading}>{readinessVerifying ? "Checking…" : "Verify providers"}</button>
                  </div>
                </section>
                <nav className="settings-tabs" aria-label="Settings sections">
                  {([
                    ["workspace", "Workspace", "What Aurora may run"],
                    ["services", "Providers", "AI and media services"],
                    ["publishing", "YouTube", "Upload and consent"],
                    ["channel", "Channel style", "Voice and schedule"],
                  ] as const).map(([section, title, description]) => (
                    <button
                      className={`settings-tab${settingsSection === section ? " is-active" : ""}`}
                      type="button"
                      key={section}
                      aria-pressed={settingsSection === section}
                      onClick={() => setSettingsSection(section)}
                    >
                      <strong>{title}</strong><small>{description}</small>
                    </button>
                  ))}
                </nav>
                {settingsSection === "workspace" && <>
                <form className="settings-section workspace-settings-form" onSubmit={saveStudioSettings}>
                  <div className="settings-section-heading">
                    <span>01</span>
                    <div><h3>Workspace defaults</h3><p>These choices apply to new work in this workspace. Channel consent and safety checks still apply.</p></div>
                  </div>
                  {studioSettingsError && <div className="message message-error" role="alert">{studioSettingsError}</div>}
                  {studioSettingsNotice && <div className="message message-success" role="status">{studioSettingsNotice}</div>}
                  {studioSettingsLoading ? <div className="settings-loading">Loading this workspace’s settings…</div> : (
                    <>
                      {!studioSettingsCanEdit && <div className="settings-empty">You have read-only access. Ask a workspace owner or admin to change these settings.</div>}
                      <fieldset disabled={!studioSettingsCanEdit} className="studio-settings-fieldset">
                      <div className="studio-setting-toggles">
                        <label className="studio-setting-toggle">
                          <input type="checkbox" checked={studioSettings.research_enabled} onChange={(event) => setStudioSettings((current) => ({ ...current, research_enabled: event.target.checked }))} />
                          <span><strong>Allow research runs</strong><small>Pause manual and scheduled research for this workspace.</small></span>
                        </label>
                        <label className="studio-setting-toggle">
                          <input type="checkbox" checked={studioSettings.video_generation_enabled} onChange={(event) => setStudioSettings((current) => ({ ...current, video_generation_enabled: event.target.checked }))} />
                          <span><strong>Allow video production</strong><small>Pause new video-generation runs. Existing runs already in progress are not cancelled.</small></span>
                        </label>
                        <label className="studio-setting-toggle studio-setting-toggle-caution">
                          <input type="checkbox" checked={studioSettings.automatic_publishing_enabled} onChange={(event) => setStudioSettings((current) => ({ ...current, automatic_publishing_enabled: event.target.checked }))} />
                          <span><strong>Allow automatic publishing</strong><small>Requires separate channel-owner consent, enabled publisher infrastructure, successful QA, and channel publishing policy.</small></span>
                        </label>
                      </div>
                      <div className="studio-setting-fields">
                        <label>Default video duration<select value={studioSettings.target_duration_seconds} onChange={(event) => setStudioSettings((current) => ({ ...current, target_duration_seconds: Number(event.target.value) as 15 | 30 | 45 }))}><option value={15}>15 seconds</option><option value={30}>30 seconds</option><option value={45}>45 seconds</option></select></label>
                        <label>Default language<input value={studioSettings.language} maxLength={80} required onChange={(event) => setStudioSettings((current) => ({ ...current, language: event.target.value }))} placeholder="English" /></label>
                        <label>Default tone<input value={studioSettings.tone} maxLength={120} required onChange={(event) => setStudioSettings((current) => ({ ...current, tone: event.target.value }))} placeholder="Clear, engaging, documentary" /></label>
                        <label className="studio-setting-audience">Default audience<input value={studioSettings.audience} maxLength={300} required onChange={(event) => setStudioSettings((current) => ({ ...current, audience: event.target.value }))} placeholder="Curious general audience" /></label>
                        <label className="studio-setting-rules">Additional content rules<textarea rows={3} value={studioSettingsRulesText} onChange={(event) => setStudioSettingsRulesText(event.target.value)} placeholder={'One preference per line, e.g.\nUse metric units\nAvoid sensational claims'} /><small>Rules guide creative output only. They cannot override evidence, rights, platform policies, or channel-specific restrictions.</small></label>
                      </div>
                      <details className="studio-setting-advanced">
                        <summary>Advanced story model controls <span>Optional · defaults are tuned for local Qwen 3 4B</span></summary>
                        <div className="studio-setting-fields studio-setting-advanced-fields">
                          <label>Qwen3 thinking<select value={studioSettings.qwen_thinking_mode} onChange={(event) => setStudioSettings((current) => ({ ...current, qwen_thinking_mode: event.target.value as "off" | "on" }))}><option value="off">Off · faster, lower memory</option><option value="on">On · deeper reasoning, slower</option></select><small>Ollama’s think/no-think switch; it is not a numeric thinking-token budget.</small></label>
                          <label>Maximum story output tokens<select value={studioSettings.story_max_output_tokens ?? "default"} onChange={(event) => setStudioSettings((current) => ({ ...current, story_max_output_tokens: event.target.value === "default" ? null : Number(event.target.value) }))}><option value="default">Provider default (Qwen 2048)</option><option value="768">768</option><option value="1024">1024</option><option value="1536">1536</option><option value="2048">2048</option><option value="3072">3072</option><option value="4096">4096</option></select><small>Maximum size of the full model reply. Higher limits can take longer and use more memory.</small></label>
                          <label>Story request timeout<select value={studioSettings.story_timeout_seconds} onChange={(event) => setStudioSettings((current) => ({ ...current, story_timeout_seconds: Number(event.target.value) }))}><option value={60}>60 seconds</option><option value={120}>120 seconds</option><option value={180}>180 seconds</option><option value={300}>300 seconds</option><option value={600}>600 seconds</option></select><small>Maximum wait for each story-model request.</small></label>
                        </div>
                      </details>
                      <div className="studio-settings-footer">
                        <span className="settings-caution">Secrets and API keys are never shown here. Provider availability is controlled by this deployment.</span>
                        <button className="button button-primary" disabled={!studioSettingsCanEdit || studioSettingsSaving}>{studioSettingsSaving ? "Saving settings…" : "Save workspace settings"}</button>
                      </div>
                      </fieldset>
                    </>
                  )}
                </form>
                </>}
                {settingsSection === "services" && <>
                {readinessError && <div className="message message-error" role="alert">{readinessError}</div>}
                <div className="settings-workflow">
                  <div className="settings-workflow-title"><span>01</span><div><strong>Connect services</strong><small>Use `.env` for provider endpoints and credentials, then restart Compose services.</small></div></div>
                  <div className="settings-provider-grid">
                    {(productionReadiness?.checks.filter((check) => ["research", "story", "media", "story_connectivity", "tts_connectivity", "comfyui_connectivity"].includes(check.key)) ?? []).map((check) => (
                      <article className="settings-provider-card" key={check.key}>
                        <div className="settings-card-top"><span className="settings-provider-icon">{check.key === "research" ? "⌕" : check.key === "story" ? "✳" : check.key.startsWith("comfyui") ? "▧" : "◉"}</span><span className={`readiness-status readiness-${check.status}`}>{check.status}</span></div>
                        <h3>{check.title}</h3><p>{check.detail}</p>
                        <small>{check.action ?? (check.key === "research" ? "Live source selection is configured in this local deployment." : check.key.startsWith("story") ? "Model provider and model ID are configured; credentials stay private." : check.key.startsWith("comfyui") ? "ComfyUI endpoint and workflow settings are deployment-level configuration." : "Narration provider and visual pipeline are configured for this deployment.")}</small>
                      </article>
                    ))}
                    {!productionReadiness && <div className="settings-loading">{readinessLoading ? "Loading runtime status…" : "Refresh status to load runtime configuration."}</div>}
                  </div>
                </div>
                <p className="settings-security-note"><LockIcon /> Provider keys stay in the local `.env` file. Restart Aurora after changing a key. Secret values are never shown in the browser.</p>
                </>}
                {settingsSection === "publishing" && <div className="settings-lower-grid">
                  <section className="settings-section">
                    <div className="settings-section-heading"><span>02</span><div><h3>YouTube publishing</h3><p>Review channel access and upload consent.</p></div></div>
                    {(productionReadiness?.checks.filter((check) => ["youtube_oauth", "channel", "publish_consent", "daily_schedule"].includes(check.key)) ?? []).map((check) => (
                      <div className="settings-check-row" key={check.key}><div><strong>{check.title}</strong><small>{check.detail}</small></div><span className={`readiness-status readiness-${check.status}`}>{check.status}</span></div>
                    ))}
                    <button className="button button-secondary button-small" onClick={() => setActivePage("channels")}>Manage channels <ArrowIcon /></button>
                    <p className="settings-caution">Automatic publishing stays off until upload, processing, and recovery are accepted on your controlled test channel.</p>
                  </section>
                </div>}
                {settingsSection === "channel" && <div className="settings-lower-grid settings-single-panel">
                  <section className="settings-section">
                    <div className="settings-section-heading"><span>03</span><div><h3>Workspace preferences</h3><p>Choose defaults that apply to this channel’s content.</p></div></div>
                    {channels.length ? channels.map((channel) => <div className="settings-channel-row" key={channel.id}><div className="channel-avatar channel-avatar-fallback"><YouTubeIcon /></div><div><strong>{channel.title}</strong><small>Voice, editorial rules, and daily schedule</small></div><button className="text-button" onClick={() => { setActivePage("channels"); void openChannelSettings(channel); }}>Configure</button></div>) : <p className="settings-empty">Connect a channel to configure its voice, editorial profile, and publishing schedule.</p>}
                  </section>
                </div>}
              </div>

              <div className="workspace-page" hidden={activePage !== "research"}>
              <section className="research-panel" aria-labelledby="research-heading">
                <div className="section-heading">
                  <div><h2 id="research-heading">Research workspace</h2><p className="muted">Create a research run and inspect its source provenance and evidence excerpts.</p></div>
                  <div className="section-heading-actions">
                    <button className="text-button refresh-button" onClick={() => void loadResearchRuns()} disabled={researchLoading}>{researchLoading ? "Refreshing…" : "↻ Refresh"}</button>
                    <button className="button button-secondary button-small" onClick={() => void cleanFailedResearchRuns()} disabled={researchCleanupBusy}>{researchCleanupBusy ? "Cleaning…" : "Clean failed runs"}</button>
                  </div>
                </div>
                <form className="research-create form-inline" onSubmit={createResearchRun}>
                  <label className="grow">Research topic<input value={researchTopic} onChange={(event) => setResearchTopic(event.target.value)} required minLength={1} maxLength={300} placeholder="e.g. Recent advances in grid-scale energy storage" /></label>
                  <button className="button button-primary" disabled={researchSubmitting || !researchTopic.trim()}>{researchSubmitting ? "Queueing…" : "Create research run"}</button>
                </form>
                <p className="research-helper">Sources use the providers configured for this local workspace. Check Overview → Production readiness for provider status.</p>
                <div className="research-workflow-note" role="note">
                  <strong>How this works</strong>
                  <span>One research run covers one topic. When it is complete, choose a YouTube channel and create a video draft. The channel profile saves your niche and editorial rules. Its daily schedule can pick fresh, niche-related topics; drafts wait for review unless you separately enable automatic publishing.</span>
                </div>
                {researchError && <div className="message message-error" role="alert">{researchError}</div>}
                {selectedResearchRun?.status === "completed" && (
                  <div className="research-production-cta">
                    <div><strong>Research is ready for a video</strong><small>Queue a script and video draft. Aurora will not upload it without your review and approval.</small></div>
                    <div className="research-production-actions">
                      <label className="visually-compact-label">Publish channel
                        <select aria-label="Channel for video production" value={productionChannelId} onChange={(event) => setProductionChannelId(event.target.value)} disabled={channelsLoading || channels.length === 0}>
                          {!channels.some((channel) => channel.can_publish) ? <option value="">Connect a channel with upload access</option> : channels.filter((channel) => channel.can_publish).map((channel) => <option key={channel.id} value={channel.id}>{channel.title} · upload ready</option>)}
                        </select>
                      </label>
                      <button className="button button-primary" type="button" onClick={() => void createProductionFromResearch()} disabled={productionQueueBusy || !productionChannelId || channelsLoading || !channels.some((channel) => channel.can_publish)}>
                        {productionQueueBusy ? "Queueing video…" : "Create video production"}
                      </button>
                    </div>
                  </div>
                )}
                {researchLoading ? (
                  <div className="channel-loading"><span className="spinner" /> Loading research runs…</div>
                ) : researchRuns.length === 0 ? (
                  <div className="empty-channels"><div className="empty-icon">⌕</div><h3>No research runs yet</h3><p className="muted">Create a topic above to queue a synthetic research run for this workspace.</p></div>
                ) : (
                  <div className="research-layout">
                    <div className="research-run-list" aria-label="Research runs">
                      {researchRuns.map((run) => (
                        <button key={run.id} className={`research-run-item ${selectedResearchRunId === run.id ? "research-run-selected" : ""}`} onClick={() => setSelectedResearchRunId(run.id)} type="button">
                          <span className={`research-status research-status-${run.status}`}>{run.status}</span>
                          <strong>{run.topic}</strong>
                          <small>{formatDate(run.created_at)}</small>
                          {run.failure_message && <small className="research-failure">{run.failure_message}</small>}
                        </button>
                      ))}
                    </div>
                    <div className="research-detail">
                      <div className="research-detail-heading"><h3>Sources</h3><span className="count-pill">{researchSources.length} loaded</span></div>
                      {!selectedResearchRunId ? <p className="muted">Select a research run.</p> : sourcesLoading ? (
                        <div className="channel-loading"><span className="spinner" /> Loading sources…</div>
                      ) : researchSources.length === 0 ? (
                        <div className="research-empty"><strong>No sources available yet</strong><span className="muted">Sources appear after the research worker completes a run.</span></div>
                      ) : (
                        <div className="research-source-list">
                          {researchSources.map((source) => (
                            <button type="button" key={source.id} className={`research-source-item ${selectedResearchSourceId === source.id ? "research-source-selected" : ""}`} onClick={() => setSelectedResearchSourceId(source.id)}>
                              <strong>{source.title}</strong>
                              <span>{source.publisher || source.source_type} · Retrieved {formatDate(source.retrieved_at)}</span>
                              <small>{source.canonical_url}</small>
                            </button>
                          ))}
                        </div>
                      )}
                      {selectedResearchSource && (
                        <details className="rights-disclosure">
                          <summary>Source rights &amp; licensing <span>{selectedResearchSource.rights_metadata.status.replaceAll("_", " ")} · expand review</span></summary>
                        <section className="rights-review-panel" aria-labelledby="source-rights-title">
                          <div className="research-detail-heading">
                            <div>
                              <h3 id="source-rights-title">Source reuse rights</h3>
                              <p className="muted">Review the exact source revision before allowing its material into a production story or media workflow.</p>
                            </div>
                            <span className={`rights-status rights-status-${selectedResearchSource.rights_metadata.status}`}>
                              {selectedResearchSource.rights_metadata.status.replaceAll("_", " ")}
                            </span>
                          </div>
                          <div className="rights-metadata-grid">
                            <div><small>Reuse scope</small><strong>{selectedResearchSource.rights_metadata.scope.replaceAll("_", " ")}</strong></div>
                            <div><small>Rights holder</small><strong>{selectedResearchSource.rights_metadata.rights_holder || "Not recorded"}</strong></div>
                            <div><small>License identifiers</small><strong>{selectedResearchSource.rights_metadata.license_ids.join(", ") || "None recorded"}</strong></div>
                            <div><small>Creators</small><strong>{selectedResearchSource.rights_metadata.creators.join(", ") || "Not recorded"}</strong></div>
                          </div>
                          <div className="rights-source-reference">
                            <small>Canonical source</small>
                            <a href={selectedResearchSource.canonical_url} target="_blank" rel="noreferrer">{selectedResearchSource.canonical_url}</a>
                            <small>Exact source revision</small>
                            {selectedResearchSource.rights_metadata.source_revision_url ? (
                              <a href={selectedResearchSource.rights_metadata.source_revision_url} target="_blank" rel="noreferrer">{selectedResearchSource.rights_metadata.source_revision_url}</a>
                            ) : <span className="muted">No revision URL supplied by the research provider.</span>}
                          </div>
                          {selectedResearchSource.rights_metadata.license_urls.length > 0 && (
                            <div className="rights-source-reference">
                              <small>Recorded license references</small>
                              {selectedResearchSource.rights_metadata.license_urls.map((url) => (
                                <a href={url} key={url} target="_blank" rel="noreferrer">{url}</a>
                              ))}
                            </div>
                          )}
                          {rightsReviewError && <div className="message message-error" role="alert">{rightsReviewError}</div>}
                          {rightsReviewNotice && <div className="message message-success" role="status">{rightsReviewNotice}</div>}
                          <details className="rights-review-disclosure">
                          <summary>Record a rights decision <span>Review rights history</span></summary>
                          <form className="rights-review-form" onSubmit={submitSourceRightsReview}>
                            <div className="rights-form-heading">
                              <strong>Record a rights decision</strong>
                              <span className="muted">Only workspace owners and admins can submit. All workspace members can inspect history.</span>
                            </div>
                            <div className="rights-form-grid">
                              <label>Decision
                                <select value={rightsDecision} onChange={(event) => setRightsDecision(event.target.value as "cleared" | "restricted")}>
                                  <option value="restricted">Restricted — block production reuse</option>
                                  <option value="cleared">Cleared — permitted within declared scope</option>
                                </select>
                              </label>
                              <label>Reuse scope
                                <select value={rightsScope} onChange={(event) => setRightsScope(event.target.value as RightsScope)}>
                                  <option value="unknown">Unknown</option>
                                  <option value="source_text">Source text</option>
                                  <option value="abstract">Abstract</option>
                                  <option value="metadata">Metadata</option>
                                  <option value="media_asset">Media asset</option>
                                </select>
                              </label>
                              <label>License identifiers
                                <input value={rightsLicenseIds} onChange={(event) => setRightsLicenseIds(event.target.value)} placeholder="CC0-1.0, CC-BY-4.0" />
                              </label>
                              <label>License URLs
                                <textarea rows={2} value={rightsLicenseUrls} onChange={(event) => setRightsLicenseUrls(event.target.value)} placeholder="One HTTPS license URL per line" />
                              </label>
                              <label>Rights holder
                                <input value={rightsHolder} onChange={(event) => setRightsHolder(event.target.value)} maxLength={500} placeholder="Rights holder or publisher" />
                              </label>
                              <label>Creators / authors
                                <input value={rightsCreators} onChange={(event) => setRightsCreators(event.target.value)} placeholder="Separate names with commas" />
                              </label>
                              <label className="rights-form-wide">Attribution text
                                <textarea rows={2} value={rightsAttribution} onChange={(event) => setRightsAttribution(event.target.value)} maxLength={2000} placeholder="Required for a cleared decision. Include creator, title and license attribution." />
                              </label>
                            </div>
                            <fieldset className="rights-restrictions">
                              <legend>Reuse restrictions</legend>
                              <div className="rights-restriction-grid">
                                {([
                                  ["attribution", "Attribution"],
                                  ["share_alike", "Share alike"],
                                  ["non_commercial", "Non-commercial"],
                                  ["no_derivatives", "No derivatives"],
                                  ["indicate_modifications", "Indicate modifications"],
                                  ["no_verbatim_reuse_without_license", "No verbatim reuse without license"],
                                  ["review_page_history", "Review page history"],
                                  ["unknown", "Unknown restriction"],
                                ] as const).map(([value, label]) => (
                                  <label className="checkbox-row" key={value}>
                                    <input
                                      type="checkbox"
                                      checked={rightsRestrictions.includes(value)}
                                      onChange={(event) => setRightsRestrictions((current) =>
                                        event.target.checked
                                          ? [...current, value]
                                          : current.filter((item) => item !== value),
                                      )}
                                    />
                                    <span>{label}</span>
                                  </label>
                                ))}
                              </div>
                            </fieldset>
                            <label>Review notes
                              <textarea rows={3} value={rightsReviewNotes} onChange={(event) => setRightsReviewNotes(event.target.value)} required minLength={1} maxLength={2000} placeholder="Explain what you verified and why this decision is appropriate." />
                            </label>
                            <label>Additional evidence URLs
                              <textarea rows={3} value={rightsEvidenceUrls} onChange={(event) => setRightsEvidenceUrls(event.target.value)} placeholder="One HTTPS evidence URL per line. The exact source revision and declared license URLs are included automatically." />
                            </label>
                            <p className="rights-review-warning">A cleared decision is not a general legal determination. Confirm the license, rights holder, scope and intended use. Restricted or unknown rights remain blocked by the final media QA gate.</p>
                            <button className="button button-primary" type="submit" disabled={rightsReviewBusy || !rightsReviewNotes.trim()}>
                              {rightsReviewBusy ? "Recording decision…" : rightsDecision === "cleared" ? "Record cleared rights" : "Record restriction"}
                            </button>
                          </form>
                          <div className="rights-history">
                            <div className="research-detail-heading">
                              <h4>Review history</h4>
                              <button className="text-button" type="button" onClick={() => void loadRightsReviews()} disabled={rightsReviewsLoading}>
                                {rightsReviewsLoading ? "Refreshing…" : "Refresh history"}
                              </button>
                            </div>
                            {rightsReviewsLoading ? (
                              <div className="channel-loading"><span className="spinner" /> Loading review history…</div>
                            ) : rightsReviews.length === 0 ? (
                              <div className="research-empty">No human rights decisions have been recorded for this source.</div>
                            ) : (
                              <div className="rights-history-list">
                                {rightsReviews.map((review) => (
                                  <article className="rights-history-item" key={review.id}>
                                    <div className="research-detail-heading">
                                      <strong>{review.decision === "cleared" ? "Rights cleared" : "Reuse restricted"}</strong>
                                      <span className={`rights-status rights-status-${review.decision}`}>{review.decision}</span>
                                    </div>
                                    <small>{formatDate(review.created_at)} · Reviewer {review.reviewer_user_id.slice(0, 8)} · Profile {review.rights_profile_id.slice(0, 8)}</small>
                                    <p>{review.review_notes}</p>
                                    <div className="rights-history-links">
                                      {review.evidence_urls.map((url) => <a href={url} key={url} target="_blank" rel="noreferrer">{url}</a>)}
                                    </div>
                                  </article>
                                ))}
                              </div>
                            )}
                          </div>
                          </details>
                        </section>
                        </details>
                      )}
                      <details className="research-evidence-disclosure">
                      <summary><strong>Evidence excerpts</strong><span className="count-pill">{researchEvidence.length} loaded</span></summary>
                      {evidenceLoading ? (
                        <div className="channel-loading"><span className="spinner" /> Loading evidence…</div>
                      ) : researchEvidence.length === 0 ? (
                        <div className="research-empty"><span className="muted">Select a source with evidence excerpts to inspect provenance.</span></div>
                      ) : (
                        <div className="research-evidence-list">
                          {researchEvidence.map((item) => <article className="research-evidence-item" key={item.id}><p>{item.excerpt}</p>{item.locator && <small>{item.locator}</small>}</article>)}
                        </div>
                      )}
                      </details>
                    </div>
                  </div>
                )}
              </section>
              </div>

              <div className="workspace-page" hidden={activePage !== "studio"}>
              <section className="research-panel" aria-labelledby="production-review-title">
                <div className="section-heading">
                  <div>
                    <h2 id="production-review-title">Production review</h2>
                    <p className="muted">Inspect generated scripts, source references and claim verification before approving a completed production run.</p>
                  </div>
                  <div className="section-heading-actions">
                    <button className="text-button refresh-button" onClick={() => { void loadProductionRuns(); void loadStoryArtifacts(); void loadMediaArtifacts(); void loadProductionPublishJob(); }} disabled={productionLoading}>
                      {productionLoading ? "Refreshing…" : "↻ Refresh"}
                    </button>
                    <button className="button button-secondary button-small" onClick={() => void cleanFailedProductionRuns()} disabled={productionCleanupBusy}>{productionCleanupBusy ? "Cleaning…" : "Clean old attempts"}</button>
                  </div>
                </div>
                {productionError && <div className="message message-error" role="alert">{productionError}</div>}
                {productionLoading ? (
                  <div className="channel-loading"><span className="spinner" /> Loading production runs…</div>
                ) : productionRuns.length === 0 ? (
                  <div className="research-empty">
                    <strong>No production runs yet</strong>
                    <span className="muted">A scheduled run appears here after its research completes, or when an editor queues production manually.</span>
                  </div>
                ) : (
                  <div className="research-layout">
                    <div className="research-run-list" aria-label="Production runs">
                      {productionRuns.map((run) => (
                        <button
                          key={run.id}
                          type="button"
                          className={`research-run-item ${selectedProductionRunId === run.id ? "research-run-selected" : ""}`}
                          onClick={() => setSelectedProductionRunId(run.id)}
                        >
                          <span className={`research-status research-status-${run.status}`}>{run.status.replaceAll("_", " ")}</span>
                          <strong>{run.scheduled_local_date ? `Scheduled · ${run.scheduled_local_date}` : `Research ${run.research_run_id.slice(0, 8)}`}</strong>
                          <small>{formatDate(run.created_at)}</small>
                          <small>{run.stages.filter((stage) => stage.status === "completed").length} / {run.stages.length} stages complete</small>
                        </button>
                      ))}
                    </div>
                    <div className="research-detail">
                      {selectedProductionRun ? (
                        <>
                          <div className="research-detail-heading">
                            <h3>Editorial artifacts</h3>
                            <span className="count-pill">{storyArtifacts.length} versions</span>
                          </div>
                          <p className="research-helper">
                            Run status: <strong>{selectedProductionRun.status.replaceAll("_", " ")}</strong>
                            {selectedProductionRun.channel_profile_version ? ` · Editorial profile v${selectedProductionRun.channel_profile_version}` : ""}
                          </p>
                          <details className="story-review-section story-review-disclosure production-stage-disclosure">
                            <summary>Production stages · {selectedProductionRun.stages.length} <span className="muted">{selectedProductionRun.stages.filter((stage) => stage.status === "completed").length} complete</span></summary>
                            <div className="story-artifact-list">
                              {selectedProductionRun.stages.map((stage) => (
                                <article className="story-beat" key={stage.id}>
                                  <div className="research-detail-heading">
                                    <b>{productionStageLabels[stage.stage_key] ?? stage.stage_key.replaceAll("_", " ")}</b>
                                    <span className={`research-status research-status-${stage.status}`}>{stage.status.replaceAll("_", " ")}</span>
                                  </div>
                                  <small>
                                    {stage.attempt_count} {stage.attempt_count === 1 ? "attempt" : "attempts"}
                                    {stage.provider_id ? ` · ${stage.provider_id}` : ""}
                                    {stage.model_id ? ` / ${stage.model_id}` : ""}
                                    {formatElapsed(stage.started_at, stage.completed_at) ? ` · elapsed ${formatElapsed(stage.started_at, stage.completed_at)}` : ""}
                                    {stage.completed_at ? ` · completed ${formatDate(stage.completed_at)}` : ""}
                                  </small>
                                  {stage.failure_code && <small className="readiness-error">Failure code: {stage.failure_code.replaceAll("_", " ")}</small>}
                                </article>
                              ))}
                            </div>
                          </details>
                          <section className="story-review-section" aria-label="Generated media">
                            <strong>Generated media ({mediaArtifacts.length})</strong>
                            {mediaArtifacts.length === 0 ? (
                              <p className="muted">No media has been generated for this run yet.</p>
                            ) : (
                              <div className="story-artifact-list">
                                {mediaArtifacts.some((item) => item.artifact_kind === "image") && (
                                  <section className="scene-review" aria-label="Generated scene stills">
                                    <div>
                                      <strong>Review the scene images</strong>
                                      <p className="muted">ComfyUI can misread a prompt. Check that every image matches its narration before approving.</p>
                                    </div>
                                    <div className="scene-review-grid">
                                      {mediaArtifacts.filter((item) => item.artifact_kind === "image").map((item, index) => (
                                        <figure key={item.id}>
                                          <img src={apiUrl(item.content_url)} alt={`Generated scene ${index + 1}`} loading="lazy" />
                                          <figcaption>Scene {index + 1}{typeof item.metadata.scene_index === "number" ? ` · ${item.metadata.scene_index + 1}` : ""}</figcaption>
                                        </figure>
                                      ))}
                                    </div>
                                  </section>
                                )}
                                {mediaArtifacts.filter((item) => item.artifact_kind === "video").map((item) => (
                                  <article className="story-artifact-card" key={item.id}>
                                    <h4>Rendered video</h4>
                                    <video className="production-video-preview" controls preload="metadata" playsInline>
                                      <source src={apiUrl(item.content_url)} type={item.mime_type} />
                                      {mediaArtifacts.filter((caption) => caption.artifact_kind === "captions").map((caption) => (
                                        <track key={caption.id} kind="captions" src={apiUrl(caption.content_url)} srcLang="en" label="English" />
                                      ))}
                                    </video>
                                    <small>{item.width}×{item.height} · {item.duration_ms ? `${Math.round(item.duration_ms / 1000)} seconds` : "duration unavailable"} · {(item.byte_size / 1048576).toFixed(1)} MB</small>
                                    <a href={apiUrl(item.content_url)} download={`aurora-${selectedProductionRun.id}.mp4`}>Download video</a>
                                  </article>
                                ))}
                                {mediaArtifacts.some((item) => item.artifact_kind !== "video") && (
                                  <details className="story-review-section story-review-disclosure supporting-media-disclosure">
                                    <summary>Audio, captions, images & thumbnail · {mediaArtifacts.filter((item) => item.artifact_kind !== "video").length} files</summary>
                                    <div className="supporting-media-links">
                                      {mediaArtifacts.filter((item) => item.artifact_kind !== "video").map((item) => (
                                        <a key={item.id} href={apiUrl(item.content_url)} download={`aurora-${item.artifact_kind}-${item.id}`}>
                                          Download {item.artifact_kind} · {(item.byte_size / 1024).toFixed(0)} KB
                                        </a>
                                      ))}
                                    </div>
                                  </details>
                                )}
                              </div>
                            )}
                          </section>
                          {storyArtifactsLoading ? (
                            <div className="channel-loading"><span className="spinner" /> Loading story artifacts…</div>
                          ) : storyArtifacts.length === 0 ? (
                            <div className="research-empty">
                              <strong>No story artifact has been generated yet</strong>
                              <span className="muted">The story worker will attach a draft and a verified version when those stages complete.</span>
                            </div>
                          ) : (
                            <div className="story-artifact-list">
                              {storyArtifacts.map((artifact) => (
                                <details className="story-artifact-card story-artifact-disclosure" key={artifact.id}>
                                  <summary>
                                    <span>
                                      <strong>{artifact.artifact_type === "story_verified" ? "Verified story" : "Story draft"} · v{artifact.version}</strong>
                                      <small>{artifact.payload.title}</small>
                                    </span>
                                    <span className="count-pill">{artifact.payload.target_duration_seconds}s</span>
                                  </summary>
                                  <div className="story-artifact-content">
                                  <p className="story-hook">{artifact.payload.hook}</p>
                                  <details className="story-review-section story-review-disclosure">
                                    <summary>Narration</summary>
                                    <p>{artifact.payload.narration}</p>
                                    <small>{artifact.payload.narration.trim().split(/\s+/).filter(Boolean).length} words · budget {artifact.payload.word_budget}</small>
                                  </details>
                                  <details className="story-review-section story-review-disclosure">
                                    <summary>Story beats · {artifact.payload.beats.length}</summary>
                                    {artifact.payload.beats.map((beat, index) => (
                                      <div className="story-beat" key={`${artifact.id}-beat-${index}`}>
                                        <b>{index + 1}. {beat.heading}</b>
                                        <p>{beat.summary}</p>
                                      </div>
                                    ))}
                                  </details>
                                  <details className="story-review-section story-review-disclosure">
                                    <summary>Scene directions · {artifact.payload.scenes.length}</summary>
                                    {artifact.payload.scenes.map((scene, index) => (
                                      <div className="story-beat" key={`${artifact.id}-scene-${index}`}>
                                        <b>Scene {index + 1} · {scene.duration_seconds}s</b>
                                        <p>{scene.visual_direction}</p>
                                        <small>{scene.narration_segment}</small>
                                      </div>
                                    ))}
                                  </details>
                                  <details className="story-review-section story-review-disclosure">
                                    <summary>Research evidence · {artifact.evidence_context.length} sources</summary>
                                    {artifact.evidence_context.length === 0 ? (
                                      <p className="muted">No source excerpts were attached to this artifact version.</p>
                                    ) : artifact.evidence_context.map((source) => (
                                      <div className="story-beat" key={`${artifact.id}-source-${source.id}`}>
                                        <b>{source.title}</b>
                                        <small>{source.publisher || source.source_type} · {source.canonical_url}</small>
                                        {source.evidence.map((excerpt) => (
                                          <div className="story-evidence-excerpt" key={excerpt.id}>
                                            <p>{excerpt.excerpt}</p>
                                            {excerpt.locator && <small>{excerpt.locator}</small>}
                                          </div>
                                        ))}
                                      </div>
                                    ))}
                                  </details>
                                  <details className="story-review-section story-review-disclosure">
                                    <summary>Claim checks · {artifact.payload.factual_claims.length}</summary>
                                    {artifact.payload.factual_claims.length === 0 ? (
                                      <p className="muted">No atomic factual claims were extracted from this version.</p>
                                    ) : artifact.payload.factual_claims.map((claim, index) => (
                                      <div className="story-claim" key={`${artifact.id}-claim-${index}`}>
                                        <span className={`claim-status claim-status-${claim.verification_status}`}>{claim.verification_status.replaceAll("_", " ")}</span>
                                        <p>{claim.claim}</p>
                                        <small>{claim.evidence.length} evidence assessments · {claim.source_ids.length} sources</small>
                                        {claim.evidence.map((item) => (
                                          <small key={item.evidence_id}>Evidence {item.evidence_id.slice(0, 8)} · {item.relation}</small>
                                        ))}
                                      </div>
                                    ))}
                                  </details>
                                  <details className="story-provenance">
                                    <summary>Provenance and integrity</summary>
                                    <p>{artifact.payload.source_references.length} source references · schema {artifact.schema_version}</p>
                                    <code>{artifact.content_hash}</code>
                                  </details>
                                  </div>
                                </details>
                              ))}
                            </div>
                          )}
                          {selectedProductionRun.status === "awaiting_review" && (
                            <div className="story-approval">
                              <h3>Review, decide and publish</h3>
                              <p className="muted">Check the full video and every scene. Set the YouTube title, description, tags and visibility below. Approve queues this exact metadata and video for upload; reject keeps the run in history.</p>
                              {productionApprovalBlocker && <p className="readiness-error" role="status">Approval unavailable: {productionApprovalBlocker}</p>}
                              {productionEditorialWarnings.length > 0 && (
                                <section className="publication-warning-list" aria-label="Publication warnings">
                                  <strong>Warnings that need your review ({productionEditorialWarnings.length})</strong>
                                  <p>These are editorial or source-rights warnings. You may resolve the underlying rights records or explicitly accept the listed warnings with the audited override below. The override does not bypass broken video files, failed technical checks, missing channel access, or a channel mismatch. If you proceed, Aurora adds a visible “License: not verified” notice to the YouTube description.</p>
                                  <ul>{productionEditorialWarnings.map((warning, index) => <li key={`${warning.kind}-${index}`}><b>{warning.kind}:</b> {warning.detail}</li>)}</ul>
                                </section>
                              )}
                              {selectedProductionRun.youtube_channel_id !== productionChannelId && (
                                <div className="channel-rebind-action">
                                  <label>Upload channel (new draft required)
                                    <select value={productionChannelId} onChange={(event) => setProductionChannelId(event.target.value)}>
                                      {channels.filter((channel) => channel.can_publish).map((channel) => <option key={channel.id} value={channel.id}>{channel.title}</option>)}
                                    </select>
                                  </label>
                                  {uploadReadyRunForSelectedChannel ? (
                                    <div className="channel-rebind-buttons">
                                      <button className="button button-primary" onClick={() => setSelectedProductionRunId(uploadReadyRunForSelectedChannel.id)}>
                                        Open upload-ready draft
                                      </button>
                                      <button className="button button-secondary" onClick={() => void recreateProductionForSelectedChannel()} disabled={productionQueueBusy || !productionChannelId}>
                                        {productionQueueBusy ? "Queueing new draft…" : "Make another from this research"}
                                      </button>
                                      <small className="muted">The new draft keeps this research and story brief, then regenerates its media so you can review it before publishing.</small>
                                    </div>
                                  ) : (
                                    <button className="button button-secondary" onClick={() => void recreateProductionForSelectedChannel()} disabled={productionQueueBusy || !productionChannelId}>
                                      {productionQueueBusy ? "Queueing new draft…" : "Create a fresh draft for this channel"}
                                    </button>
                                  )}
                                </div>
                              )}
                              <div className="publish-metadata-form">
                                <h4>YouTube upload details</h4>
                                <label>Title<input value={publishTitle} maxLength={100} onChange={(event) => setPublishTitle(event.target.value)} placeholder="Video title" /></label>
                                <label>Description<textarea value={publishDescription} maxLength={5000} rows={4} onChange={(event) => setPublishDescription(event.target.value)} placeholder="Description; Aurora appends source credits required by the video record." /></label>
                                <label>Tags, comma separated<input value={publishTags} onChange={(event) => setPublishTags(event.target.value)} placeholder="AI, GPU, technology" /></label>
                                <label>Visibility<select value={publishPrivacy} onChange={(event) => setPublishPrivacy(event.target.value as "private" | "unlisted" | "public")}>
                                  <option value="private">Private · only you can view</option><option value="unlisted">Unlisted · link holders can view</option><option value="public">Public · visible to everyone</option>
                                </select></label>
                              </div>
                              <div className="story-approval-actions">
                                {!productionEditorialWarnings.length && (
                                  <button className="button button-primary" onClick={() => void approveProductionRun()} disabled={productionActionBusy || Boolean(productionHardBlocker) || !publishTitle.trim()}>
                                    {productionActionBusy ? "Approving and queueing…" : productionHardBlocker ? "Upload unavailable" : `Approve & upload ${publishPrivacy}`}
                                  </button>
                                )}
                                {canOverrideEditorialWarnings && (
                                  <div className="editorial-override">
                                    <label className="override-confirm"><input type="checkbox" checked={warningOverrideAcknowledged} onChange={(event) => setWarningOverrideAcknowledged(event.target.checked)} /> I reviewed the video, scenes and warnings above, and accept responsibility for proceeding.</label>
                                    <label>Why are you overriding these warnings?<textarea value={warningOverrideReason} maxLength={1000} rows={2} onChange={(event) => setWarningOverrideReason(event.target.value)} placeholder="Describe your review and why this upload should proceed (at least 10 characters)." /></label>
                                    <button className="button button-warning-override" onClick={() => void approveProductionRun(true)} disabled={productionActionBusy || !warningOverrideAcknowledged || warningOverrideReason.trim().length < 10 || !publishTitle.trim()}>
                                      {productionActionBusy ? "Recording approval and queueing…" : `Approve & upload ${publishPrivacy} despite warnings`}
                                    </button>
                                  </div>
                                )}
                                {productionHardBlocker && (
                                  <p className="readiness-error">Upload cannot proceed: {productionHardBlocker} A warning override cannot replace a valid OAuth grant for the channel that owns this run.</p>
                                )}
                                <button className="button button-danger-ghost" onClick={() => void rejectProductionRun()} disabled={productionActionBusy}>
                                  Reject video
                                </button>
                              </div>
                            </div>
                          )}
                          {selectedProductionRun.status === "rejected" && (
                            <div className="story-approval" role="status">
                              <h3>Rejected for publishing</h3>
                              <p className="muted">{selectedProductionRun.rejection_reason || "This video was rejected by an editor."} The rendered files remain available in the run history; this run cannot be uploaded.</p>
                            </div>
                          )}
                          {selectedProductionRun.status === "approved" && selectedProductionRun.approved_at && (
                            <section className="story-approval" aria-label="YouTube publishing">
                              <h3>YouTube publishing</h3>
                              <p className="muted">
                                {selectedProductionRun.approval_mode === "automatic_consent"
                                  ? `Automatically approved ${formatDate(selectedProductionRun.approved_at)} under channel-owner consent.`
                                  : `Approved ${formatDate(selectedProductionRun.approved_at)}. Review the upload details and queue the video when ready.`}
                              </p>
                              {publishJob ? (
                                <div className="story-beat">
                                  <div className="research-detail-heading"><strong>{publishJob.title}</strong><span className={`research-status research-status-${publishJob.status}`}>{publishJob.status.replaceAll("_", " ")}</span></div>
                                  <p className="publish-destination">Destination: {channels.find((channel) => channel.id === selectedProductionRun.youtube_channel_id)?.title ?? "connected YouTube channel"} · Visibility: {publishJob.privacy_status}</p>
                                  {publishStatusError && <p className="publish-status-stale" role="status">{publishStatusError}</p>}
                                  {publishJob.status === "reconciliation_required" && publishJob.youtube_video_id && (
                                    <div className="publish-access-notice" role="status">
                                      <strong>Video received by YouTube · visibility needs confirmation</strong>
                                      <p>YouTube received the complete file and created a video ID. Aurora could not confirm the requested {publishJob.privacy_status} visibility, so this job is paused for recovery. Do not upload the video again.</p>
                                      {selectedRunChannel?.granted_scopes.includes(YOUTUBE_UPLOAD_SCOPE) && !selectedRunChannel.granted_scopes.includes(YOUTUBE_VIDEO_WRITE_SCOPE) && (
                                        <p>The current Google grant includes upload access, but is missing the video-update permission Aurora needs to finish publishing.</p>
                                      )}
                                    </div>
                                  )}
                                  {(["queued", "initiating", "uploading", "processing", "uploaded"].includes(publishJob.status)) && (
                                    <div className="publish-progress" role="status" aria-live="polite">
                                      <strong>
                                        {publishJob.status === "queued" && "Queued — waiting for the publisher worker"}
                                        {publishJob.status === "initiating" && "Connecting to YouTube"}
                                        {publishJob.status === "uploading" && "Uploading video to YouTube"}
                                        {publishJob.status === "processing" && "YouTube is processing the video"}
                                        {publishJob.status === "uploaded" && "Upload accepted — waiting for YouTube processing"}
                                      </strong>
                                      <span>
                                        {publishJob.status === "uploading" && publishJob.byte_size > 0
                                          ? `${Math.min(100, Math.round((publishJob.bytes_uploaded / publishJob.byte_size) * 100))}% uploaded`
                                          : `Job ${publishJob.id.slice(0, 8)} · attempt ${publishJob.attempt_count}`}
                                      </span>
                                      {publishJob.status === "uploading" && publishJob.byte_size > 0 && (
                                        <progress max={publishJob.byte_size} value={Math.min(publishJob.bytes_uploaded, publishJob.byte_size)} />
                                      )}
                                      <small>Refreshes every 3 seconds. You can leave this page open; repeated clicks are disabled while the same request is in flight.</small>
                                    </div>
                                  )}
                                  {publishJob.failure_message && <p className="readiness-error">{publishJob.failure_message}</p>}
                                  {publishJob.thumbnail_upload_warning && <p className="publish-warning">Thumbnail warning: {publishJob.thumbnail_upload_warning}</p>}
                                  {publishJob.youtube_video_id && <a href={`https://www.youtube.com/watch?v=${encodeURIComponent(publishJob.youtube_video_id)}`} target="_blank" rel="noreferrer">Open video on YouTube</a>}
                                  {publishJob.published_at && <small>Uploaded {formatDate(publishJob.published_at)}</small>}
                                  {publishJob.status === "reconciliation_required" && publishJob.youtube_video_id && (
                                    <div className="publish-reconciliation-action">
                                      <p className="muted">This recovery resumes the existing YouTube video; it does not send the video file again. After reconnecting, Aurora will check processing and apply the selected visibility.</p>
                                      {!selectedRunChannel?.can_publish && selectedProductionRun.youtube_channel_id && (
                                        <button className="button button-secondary" onClick={() => void connectYouTube("publish", selectedProductionRun.youtube_channel_id!)} disabled={loading}>
                                          {loading ? "Opening Google consent…" : "Reconnect channel for publishing"}
                                        </button>
                                      )}
                                      <button className="button button-primary" onClick={() => void reconcileYouTubeUpload()} disabled={publishActionBusy || !selectedRunChannel?.can_publish}>
                                        {publishActionBusy ? "Resuming existing video…" : selectedRunChannel?.can_publish ? "Resume existing video (no re-upload)" : "Reconnect before resuming"}
                                      </button>
                                    </div>
                                  )}
                                  {publishJob.status === "failed" || publishJob.status === "cancelled" ? (
                                    <button className="button button-primary" onClick={() => void queueYouTubeUpload(undefined, true)} disabled={publishActionBusy || !publishTitle.trim()}>
                                      {publishActionBusy ? "Retrying upload…" : `Retry upload now as ${publishPrivacy}`}
                                    </button>
                                  ) : null}
                                </div>
                              ) : (
                                <div className="story-review-section">
                                  {productionHardBlocker && <div className="publish-access-notice"><strong>Publishing access needs attention</strong><p>{productionHardBlocker}</p>{selectedProductionRun.youtube_channel_id && !selectedRunChannel?.can_publish && <button className="button button-secondary button-small" onClick={() => void connectYouTube("publish", selectedProductionRun.youtube_channel_id!)} disabled={loading}>{loading ? "Opening Google…" : "Reconnect with publishing access"}</button>}</div>}
                                  <label>Video title<input value={publishTitle} maxLength={100} onChange={(event) => setPublishTitle(event.target.value)} /></label>
                                  <label>Description<textarea value={publishDescription} maxLength={5000} rows={5} onChange={(event) => setPublishDescription(event.target.value)} /></label>
                                  <label>Tags, separated by commas<input value={publishTags} onChange={(event) => setPublishTags(event.target.value)} placeholder="Optional" /></label>
                                  <label>Visibility<select value={publishPrivacy} onChange={(event) => setPublishPrivacy(event.target.value as "private" | "unlisted" | "public")}>
                                    <option value="private">Private</option><option value="unlisted">Unlisted</option><option value="public">Public</option>
                                  </select></label>
                                  <p className="muted">Upload now sends this approved video immediately. Choose its visibility above; no daily schedule is required.</p>
                                  <button className="button button-primary" onClick={() => void queueYouTubeUpload()} disabled={publishActionBusy || Boolean(productionHardBlocker) || !publishTitle.trim()} aria-busy={publishActionBusy}>
                                    {publishActionBusy ? "Creating upload job…" : `Upload now as ${publishPrivacy}`}
                                  </button>
                                </div>
                              )}
                            </section>
                          )}
                        </>
                      ) : <p className="muted">Select a production run to review.</p>}
                    </div>
                  </div>
                )}
              </section>
              </div>
            </>
          )}

          <footer className="page-footer"><span>AURORA STUDIO</span><span>Secure integrations for your creative workflow</span><span>v0.1.0</span></footer>
        </section>
      </main>
    </div>
  );
}

function Brand() {
  return <div className="brand"><div className="brand-mark">A</div><span>AURORA<span className="brand-light"> STUDIO</span></span></div>;
}

function Message({ notice, error }: { notice: string; error: string }) {
  if (!notice && !error) return null;
  return <div className={`message ${error ? "message-error" : "message-success"}`} role="status">{error || notice}</div>;
}

function ArrowIcon() {
  return <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 10h11M10 5l5 5-5 5" /></svg>;
}
function LogoutIcon() {
  return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10 17l5-5-5-5M15 12H3M12 3h6a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-6" /></svg>;
}
function LockIcon() {
  return <svg viewBox="0 0 20 20" aria-hidden="true"><rect x="4" y="8" width="12" height="9" rx="2" /><path d="M7 8V5a3 3 0 0 1 6 0v3" /></svg>;
}
function YouTubeIcon() {
  return <svg viewBox="0 0 28 20" aria-hidden="true"><rect x="1" y="1" width="26" height="18" rx="5" /><path className="youtube-play" d="M12 6l7 4-7 4z" /></svg>;
}

export default App;
