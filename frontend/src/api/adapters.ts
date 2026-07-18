import type { components } from "./schema";
import type {
  BookSummary,
  ExportArtifact,
  JobState,
  Locale,
  ProjectBinding,
  ProviderStatus,
  ReferenceLibrary,
  Segment,
  Session,
  TranslationJob,
} from "../types/api";

export type RawProvider = components["schemas"]["ProviderState"];
export type RawLibrary = components["schemas"]["LibraryView"];
export type RawProject = components["schemas"]["ProjectView"];
export type RawJob = components["schemas"]["JobView"];
export type RawExport = components["schemas"]["ExportView"];
export type RawUpload = components["schemas"]["UploadView"];
export type RawUploadComplete = components["schemas"]["UploadCompleteView"];
export type RawProjectCreate = components["schemas"]["ProjectCreate"];
export type RawSegmentEdit = components["schemas"]["SegmentEdit"];
export type RawExportCreate = components["schemas"]["ExportCreate"];

export interface RawBook {
  id: string;
  title: string;
  filename: string;
  format: string;
  language: string;
  parse_status: string;
  source_version: number;
  created_at: string;
  updated_at?: string;
  section_count?: number;
  metadata?: Record<string, unknown>;
}

export interface RawSession {
  owner: { email: string; identity_provider: string };
  csrf_token: string;
  service: { status: string; database?: string; provider_configured?: boolean };
}

export interface RawSegment {
  block_id: string;
  stable_id: string;
  section_id: string;
  section_title: string;
  kind: string;
  source_text: string;
  translation: string | null;
  revision_kind: string | null;
  locked: boolean;
  context_version: number | null;
  candidates?: Array<{
    id: string;
    text: string;
    kind: string;
    source: string;
    revision_no: number;
    context_version: number;
  }>;
}

export interface RawSegmentPage {
  items: RawSegment[];
  page: number;
  page_size: number;
  total: number;
}

function asLocale(value: string): Locale {
  return value === "zh-TW" ? "zh-TW" : "zh-CN";
}

function coverPolicy(value: string): "preserve" | "replace" | "none" {
  if (value === "replace" || value === "none") return value;
  return "preserve";
}

export function adaptSession(raw: RawSession): Session {
  const expires = new Date(Date.now() + 15 * 60 * 1000).toISOString();
  return {
    owner: {
      email: raw.owner.email,
      display_name: null,
      identity_provider: raw.owner.identity_provider,
    },
    csrf_token: raw.csrf_token,
    csrf_expires_at: expires,
    service: {
      status: raw.service.status === "ready" && raw.service.database !== "unavailable" ? "ready" : "degraded",
      version: import.meta.env.VITE_APP_VERSION ?? "0.1.0",
    },
  };
}

export function adaptProject(raw: RawProject): ProjectBinding {
  return {
    id: raw.id,
    book_id: raw.book_id,
    name: raw.name,
    target_locale: asLocale(raw.target_locale),
    cover_policy: coverPolicy(raw.cover_policy),
    status: raw.status,
    updated_at: raw.updated_at,
  };
}

function bookState(raw: RawBook, projects: RawProject[]): BookSummary["state"] {
  if (raw.parse_status === "failed") return "failed";
  if (["uploaded", "queued", "parsing", "validating"].includes(raw.parse_status)) return "parsing";
  if (!projects.length) return "ready";
  if (projects.some((project) => project.status === "completed")) return "completed";
  if (projects.some((project) => ["awaiting_review", "context_changed"].includes(project.status))) return "review";
  if (projects.some((project) => ["translating", "reviewing", "assembling", "validating_output"].includes(project.status))) return "translating";
  return "ready";
}

export function adaptBooks(rawBooks: RawBook[], rawProjects: RawProject[]): BookSummary[] {
  return rawBooks.map((raw) => {
    const projects = rawProjects.filter((project) => project.book_id === raw.id);
    const latestProject = [...projects].sort((left, right) => right.updated_at.localeCompare(left.updated_at))[0];
    const locales = [...new Set(projects.map((project) => asLocale(project.target_locale)))] as Locale[];
    return {
      id: raw.id,
      title: raw.title || raw.filename,
      author: typeof raw.metadata?.author === "string" ? raw.metadata.author : null,
      source_format: raw.format === "txt" || raw.format === "pdf" ? raw.format : "epub",
      source_language: raw.language || "ja",
      target_locales: locales,
      cover_policy: latestProject ? coverPolicy(latestProject.cover_policy) : "preserve",
      state: bookState(raw, projects),
      section_count: typeof raw.section_count === "number" ? raw.section_count : null,
      updated_at: latestProject?.updated_at ?? raw.updated_at ?? raw.created_at,
    };
  });
}

export function adaptLibrary(raw: RawLibrary): ReferenceLibrary {
  const alignmentState: ReferenceLibrary["alignment_state"] = raw.mode === "style_only"
    ? "not_applicable"
    : raw.status === "ready" ? "ready" : raw.status === "awaiting_review" ? "review" : "pending";
  return {
    id: raw.id,
    name: raw.name,
    mode: raw.mode === "paired" ? "paired" : "style_only",
    target_locale: asLocale(raw.target_locale),
    priority: raw.priority,
    source_count: raw.source_upload_ids.length,
    alignment_state: alignmentState,
    external_processing_allowed: raw.allows_external_snippets,
    rights_confirmed_at: raw.confirmed_at,
    note: raw.notes,
  };
}

const jobStates = new Set<JobState>([
  "queued", "validating", "parsing", "awaiting_review", "translating", "reviewing", "assembling",
  "validating_output", "completed", "paused", "failed", "cancelled",
]);

function checkpointNumber(checkpoint: Record<string, unknown>, key: string): number | null {
  return typeof checkpoint[key] === "number" ? checkpoint[key] as number : null;
}

export function adaptJobs(rawJobs: RawJob[], rawProjects: RawProject[], rawBooks: RawBook[]): TranslationJob[] {
  return rawJobs.map((raw) => {
    const project = rawProjects.find((item) => item.id === raw.project_id);
    const book = project ? rawBooks.find((item) => item.id === project.book_id) : undefined;
    const total = checkpointNumber(raw.checkpoint, "total_blocks");
    const completed = checkpointNumber(raw.checkpoint, "completed_blocks") ?? (total ? Math.floor(raw.progress * total) : 0);
    const state = jobStates.has(raw.state as JobState) ? raw.state as JobState : "failed";
    return {
      id: raw.id,
      project_id: raw.project_id,
      book_id: project?.book_id ?? "",
      book_title: book?.title ?? project?.name ?? "书稿信息不可用",
      locale: project ? asLocale(project.target_locale) : "zh-CN",
      state,
      current_section: typeof raw.checkpoint.section_title === "string" ? raw.checkpoint.section_title : raw.current_stage || null,
      completed_blocks: completed,
      total_blocks: total,
      issue_count: checkpointNumber(raw.checkpoint, "issue_count") ?? (raw.error_code ? 1 : 0),
      updated_at: raw.updated_at,
    };
  });
}

export function adaptSegments(raw: RawSegmentPage, locale: Locale): Segment[] {
  return raw.items.map((item, index) => {
    const current = item.translation === null ? [] : [{
      id: `${item.block_id}:${item.context_version ?? 0}`,
      kind: item.revision_kind === "human" ? "human" as const : item.revision_kind === "reviewed" ? "reviewed" as const : "model_draft" as const,
      text: item.translation,
      created_at: "",
    }];
    const candidates = (item.candidates ?? []).map((candidate) => ({
      id: candidate.id,
      kind: "candidate" as const,
      text: candidate.text,
      created_at: "",
    }));
    return {
    id: item.block_id,
    section_id: item.section_id,
    section_title: item.section_title,
    ordinal: index + 1 + Math.max(0, raw.page - 1) * raw.page_size,
    source_text: item.source_text,
    target_text: item.translation ?? "",
    target_locale: locale,
    locked: item.locked,
    status: item.translation === null ? "pending" : item.revision_kind === "human" || item.locked ? "reviewed" : "translated",
    warnings: [],
    terminology: [],
    revisions: [...candidates, ...current],
  };
  });
}

function booleanCapability(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

export function adaptProvider(raw: RawProvider): ProviderStatus {
  return {
    configured: raw.configured,
    base_url: raw.base_url ?? null,
    key_suffix: raw.api_key_tail ?? null,
    translation_model: raw.translation_model ?? null,
    review_model: raw.review_model ?? null,
    last_verified_at: raw.last_validated_at ?? null,
    capabilities: {
      models_endpoint: booleanCapability(raw.capabilities?.models),
      json_response_format: booleanCapability(raw.capabilities?.json_response_format),
      streaming: booleanCapability(raw.capabilities?.streaming),
    },
  };
}

export function adaptExport(raw: RawExport): ExportArtifact {
  const message = typeof raw.validation.message === "string" ? raw.validation.message : null;
  const status: ExportArtifact["status"] = raw.status === "completed" ? "ready" : raw.status === "failed" ? "failed" : raw.status === "building" ? "building" : "queued";
  return {
    id: raw.id,
    book_id: "",
    locale: asLocale(raw.locale),
    format: raw.format === "txt" || raw.format === "pdf" ? raw.format : "epub",
    status,
    sha256: raw.sha256,
    created_at: raw.created_at,
    download_url: status === "ready" ? `/api/exports/${encodeURIComponent(raw.id)}/download` : null,
    validation_message: message,
  };
}
