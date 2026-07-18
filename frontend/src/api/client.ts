import { bytesToHex } from "@noble/hashes/utils";
import { sha256 } from "@noble/hashes/sha256";
import type {
  ApiProblemBody,
  BookSummary,
  ExportArtifact,
  Page,
  ProjectBinding,
  ProviderStatus,
  ProviderUpdate,
  ReferenceLibrary,
  SegmentPage,
  Session,
  TranslationJob,
} from "../types/api";
import { ApiError } from "../types/api";
import {
  adaptBooks,
  adaptExport,
  adaptJobs,
  adaptLibrary,
  adaptProject,
  adaptProvider,
  adaptSegments,
  adaptSession,
  type RawBook,
  type RawExport,
  type RawExportCreate,
  type RawJob,
  type RawLibrary,
  type RawProject,
  type RawProjectCreate,
  type RawProvider,
  type RawSegmentEdit,
  type RawSegmentPage,
  type RawSession,
  type RawUpload,
  type RawUploadComplete,
} from "./adapters";
import type { Locale } from "../types/api";

const API_ROOT = "/api";
export const UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024;

let csrfToken: string | null = null;

export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

async function parseProblem(response: Response): Promise<ApiError> {
  let body: ApiProblemBody = {};
  try {
    body = (await response.json()) as ApiProblemBody;
  } catch {
    // The edge deliberately avoids reflecting arbitrary upstream bodies.
  }
  return new ApiError(
    response.status,
    body.error?.code ?? body.code ?? `HTTP_${response.status}`,
    body.error?.message ?? body.detail ?? body.message ?? "请求未完成，请稍后重试。",
    body.request_id,
  );
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body && !(init.body instanceof FormData) && !(init.body instanceof Blob)) {
    headers.set("Content-Type", "application/json");
  }
  const method = (init.method ?? "GET").toUpperCase();
  if (!["GET", "HEAD", "OPTIONS"].includes(method) && csrfToken) {
    headers.set("X-CSRF-Token", csrfToken);
  }
  const response = await fetch(`${API_ROOT}${path}`, {
    ...init,
    headers,
    credentials: "same-origin",
    redirect: "error",
  });
  if (!response.ok) throw await parseProblem(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function pageOf<T>(items: T[]): Page<T> {
  return { items, next_cursor: null };
}

async function rawProjects(): Promise<RawProject[]> {
  return request<RawProject[]>("/projects");
}

async function projectFor(bookId: string, locale: string): Promise<RawProject | null> {
  const projects = await rawProjects();
  return projects.find((project) => project.book_id === bookId && project.target_locale === locale) ?? null;
}

export const api = {
  async session(): Promise<Session> {
    const value = adaptSession(await request<RawSession>("/session"));
    setCsrfToken(value.csrf_token);
    return value;
  },

  async books(): Promise<Page<BookSummary>> {
    const [books, projects] = await Promise.all([request<RawBook[]>("/books"), rawProjects()]);
    return pageOf(adaptBooks(books, projects));
  },

  async libraries(): Promise<Page<ReferenceLibrary>> {
    return pageOf((await request<RawLibrary[]>("/libraries")).map(adaptLibrary));
  },

  async jobs(): Promise<Page<TranslationJob>> {
    const [jobs, projects, books] = await Promise.all([
      request<RawJob[]>("/jobs"),
      rawProjects(),
      request<RawBook[]>("/books"),
    ]);
    return pageOf(adaptJobs(jobs, projects, books));
  },

  async project(bookId: string, locale: string): Promise<ProjectBinding | null> {
    const project = await projectFor(bookId, locale);
    return project ? adaptProject(project) : null;
  },

  async createProject(
    bookId: string,
    bookTitle: string,
    locale: Locale,
    cover: "preserve" | "replace" | "none",
    replacementCoverUploadId?: string,
  ): Promise<ProjectBinding> {
    const payload: RawProjectCreate = {
      book_id: bookId,
      name: `${bookTitle} · ${locale === "zh-TW" ? "繁体中文（台湾）" : "简体中文"}`,
      target_locale: locale,
      cover_policy: cover,
      replacement_cover_upload_id: replacementCoverUploadId ?? null,
      selected_library_ids: [],
      glossary: {},
      quality_mode: "two_pass",
    };
    return adaptProject(await request<RawProject>("/projects", { method: "POST", body: JSON.stringify(payload) }));
  },

  async segments(bookId: string, locale: Locale): Promise<SegmentPage> {
    const project = await projectFor(bookId, locale);
    if (!project) return { items: [], next_cursor: null, project_id: null };
    const pageSize = 200;
    const raw = await request<RawSegmentPage>(
      `/books/${encodeURIComponent(bookId)}/segments?project_id=${encodeURIComponent(project.id)}&page=1&page_size=${pageSize}`,
    );
    const hasMore = raw.total > raw.page * raw.page_size;
    return { items: adaptSegments(raw, locale), next_cursor: hasMore ? "2" : null, project_id: project.id };
  },

  async updateSegment(bookId: string, projectId: string, segmentId: string, text: string, locked: boolean): Promise<void> {
    const payload: RawSegmentEdit = { project_id: projectId, text, locked };
    await request(`/books/${encodeURIComponent(bookId)}/segments/${encodeURIComponent(segmentId)}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
  },

  async retranslateSegment(bookId: string, projectId: string, segmentId: string): Promise<void> {
    await request(`/books/${encodeURIComponent(bookId)}/segments/${encodeURIComponent(segmentId)}/retranslate`, {
      method: "POST",
      body: JSON.stringify({ project_id: projectId }),
    });
  },

  async jobAction(jobId: string, action: "pause" | "resume" | "cancel" | "retry"): Promise<void> {
    await request<RawJob>(`/jobs/${encodeURIComponent(jobId)}/${action}`, {
      method: "POST",
      body: JSON.stringify({}),
    });
  },

  async provider(): Promise<ProviderStatus> {
    return adaptProvider(await request<RawProvider>("/settings/provider"));
  },

  async updateProvider(value: ProviderUpdate): Promise<ProviderStatus> {
    return adaptProvider(await request<RawProvider>("/settings/provider", { method: "PUT", body: JSON.stringify(value) }));
  },

  async testProvider(): Promise<ProviderStatus> {
    return adaptProvider(await request<RawProvider>("/settings/provider/test", { method: "POST", body: JSON.stringify({}) }));
  },

  async exports(bookId: string, locale: Locale): Promise<Page<ExportArtifact>> {
    const project = await projectFor(bookId, locale);
    if (!project) return pageOf([]);
    const exports = await request<RawExport[]>(`/exports?project_id=${encodeURIComponent(project.id)}`);
    return pageOf(exports.map((item) => ({ ...adaptExport(item), book_id: bookId })));
  },

  async createExport(bookId: string, locale: Locale, format: "epub" | "txt" | "pdf"): Promise<ExportArtifact> {
    const project = await projectFor(bookId, locale);
    if (!project) throw new ApiError(409, "PROJECT_REQUIRED", "请先为这个地区建立译稿项目。");
    const payload: RawExportCreate = { project_id: project.id, format, parameters: {} };
    return { ...adaptExport(await request<RawExport>("/exports", { method: "POST", body: JSON.stringify(payload) })), book_id: bookId };
  },

  async uploadBook(
    file: File,
    onProgress?: (progress: number) => void,
    purpose: "book" | "cover" = "book",
  ): Promise<{ upload_id: string; book_id: string | null }> {
    let mediaType = file.type || "application/octet-stream";
    if (purpose === "cover") {
      const extension = file.name.toLowerCase().match(/\.(jpe?g|png)$/)?.[1];
      const expected = extension === "png" ? "image/png" : extension === "jpg" || extension === "jpeg" ? "image/jpeg" : null;
      if (!expected || (file.type && file.type !== expected)) {
        throw new ApiError(422, "INVALID_COVER", "替代封面必须是扩展名与内容类型一致的 JPEG 或 PNG 文件。");
      }
      mediaType = expected;
    }
    const totalChunks = Math.max(1, Math.ceil(file.size / UPLOAD_CHUNK_SIZE));
    const wholeFileHash = sha256.create();
    for (let index = 0; index < totalChunks; index += 1) {
      const start = index * UPLOAD_CHUNK_SIZE;
      const end = Math.min(file.size, start + UPLOAD_CHUNK_SIZE);
      wholeFileHash.update(new Uint8Array(await file.slice(start, end).arrayBuffer()));
      onProgress?.(((index + 1) / totalChunks) * 0.1);
    }
    const fileHash = bytesToHex(wholeFileHash.digest());
    const created = await request<RawUpload>("/uploads", {
      method: "POST",
      body: JSON.stringify({ purpose, filename: file.name, size: file.size, media_type: mediaType, sha256: fileHash }),
    });
    const uploadId = created.id;
    if (!uploadId) throw new ApiError(500, "INVALID_UPLOAD_RESPONSE", "服务器未返回上传编号。");

    for (let index = 0; index < totalChunks; index += 1) {
      const start = index * UPLOAD_CHUNK_SIZE;
      const end = Math.min(file.size, start + UPLOAD_CHUNK_SIZE);
      const bytes = new Uint8Array(await file.slice(start, end).arrayBuffer());
      const chunkHash = bytesToHex(sha256(bytes));
      const response = await fetch(`${API_ROOT}/uploads/${encodeURIComponent(uploadId)}/chunks/${index}`, {
        method: "PUT",
        headers: {
          "Content-Type": "application/octet-stream",
          "X-CSRF-Token": csrfToken ?? "",
          "X-Chunk-SHA256": chunkHash,
          "Idempotency-Key": `${uploadId}:${index}:${chunkHash}`,
          "Content-Range": `bytes ${start}-${Math.max(start, end - 1)}/${file.size}`,
        },
        body: bytes,
        credentials: "same-origin",
        redirect: "error",
      });
      if (!response.ok) throw await parseProblem(response);
      onProgress?.(0.1 + ((index + 1) / totalChunks) * 0.9);
    }

    const completed = await request<RawUploadComplete>(`/uploads/${encodeURIComponent(uploadId)}/complete`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    return { upload_id: completed.upload.id, book_id: completed.book_id };
  },
};
