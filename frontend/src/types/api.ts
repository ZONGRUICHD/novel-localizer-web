export type Locale = "zh-CN" | "zh-TW";
export type SourceFormat = "epub" | "txt" | "pdf";
export type JobState =
  | "queued"
  | "validating"
  | "parsing"
  | "awaiting_review"
  | "translating"
  | "reviewing"
  | "assembling"
  | "validating_output"
  | "completed"
  | "paused"
  | "failed"
  | "cancelled";

export interface Session {
  owner: {
    email: string;
    display_name: string | null;
    identity_provider: "github" | "google" | string;
  };
  csrf_token: string;
  csrf_expires_at: string;
  service: {
    status: "ready" | "degraded" | "maintenance";
    version: string;
  };
}

export interface BookSummary {
  id: string;
  title: string;
  author: string | null;
  source_format: SourceFormat;
  source_language: "ja" | "zh-CN" | "zh-TW" | string;
  target_locales: Locale[];
  cover_policy: "preserve" | "replace" | "none";
  state: "imported" | "parsing" | "ready" | "translating" | "review" | "completed" | "failed";
  section_count: number | null;
  updated_at: string;
  is_demo?: boolean;
}

export interface ProjectBinding {
  id: string;
  book_id: string;
  name: string;
  target_locale: Locale;
  cover_policy: "preserve" | "replace" | "none";
  status: string;
  updated_at: string;
}

export interface ReferenceLibrary {
  id: string;
  name: string;
  mode: "paired" | "style_only";
  target_locale: Locale;
  priority: number;
  source_count: number;
  alignment_state: "not_applicable" | "pending" | "review" | "ready";
  external_processing_allowed: boolean;
  rights_confirmed_at: string | null;
  note: string | null;
  is_demo?: boolean;
}

export interface TranslationJob {
  id: string;
  book_id: string;
  book_title: string;
  locale: Locale;
  state: JobState;
  current_section: string | null;
  completed_blocks: number;
  total_blocks: number | null;
  issue_count: number;
  updated_at: string;
  is_demo?: boolean;
  project_id?: string;
}

export interface SegmentRevision {
  id: string;
  kind: "model_draft" | "reviewed" | "human" | "candidate";
  text: string;
  created_at: string;
}

export interface Segment {
  id: string;
  section_id: string;
  section_title: string;
  ordinal: number;
  source_text: string;
  target_text: string;
  target_locale: Locale;
  locked: boolean;
  status: "pending" | "translated" | "reviewed" | "warning";
  warnings: string[];
  terminology: Array<{ source: string; target: string; note?: string }>;
  revisions: SegmentRevision[];
}

export interface ProviderStatus {
  configured: boolean;
  base_url: string | null;
  key_suffix: string | null;
  translation_model: string | null;
  review_model: string | null;
  last_verified_at: string | null;
  capabilities: {
    models_endpoint: boolean | null;
    json_response_format: boolean | null;
    streaming: boolean | null;
  };
}

export interface ProviderUpdate {
  base_url: string;
  api_key?: string;
  translation_model: string;
  review_model: string;
}

export interface ExportArtifact {
  id: string;
  book_id: string;
  locale: Locale;
  format: "epub" | "txt" | "pdf";
  status: "queued" | "building" | "ready" | "failed";
  sha256: string | null;
  created_at: string;
  download_url: string | null;
  validation_message: string | null;
}

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

export interface SegmentPage extends Page<Segment> {
  project_id: string | null;
}

export interface ApiProblemBody {
  code?: string;
  detail?: string;
  message?: string;
  request_id?: string;
  error?: {
    code?: string;
    message?: string;
    details?: unknown;
  };
}

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
    public readonly requestId?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}
