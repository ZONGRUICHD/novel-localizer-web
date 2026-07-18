import { describe, expect, it, vi } from "vitest";
import { File as NodeFile } from "node:buffer";
import { adaptBooks, adaptLibrary, adaptProvider, adaptSegments, adaptSession, type RawBook, type RawLibrary, type RawProject } from "./adapters";
import { api } from "./client";

const rawBook: RawBook = {
  id: "book-1",
  title: "縦書きの本",
  filename: "vertical.epub",
  format: "epub",
  language: "ja",
  parse_status: "ready",
  source_version: 1,
  created_at: "2026-07-19T00:00:00Z",
};

const rawProject: RawProject = {
  id: "project-cn",
  book_id: "book-1",
  name: "简中译稿",
  target_locale: "zh-CN",
  cover_policy: "preserve",
  replacement_cover_upload_id: null,
  selected_library_ids: [],
  glossary: {},
  translation_model: "translate-model",
  review_model: "review-model",
  quality_mode: "two_pass",
  context_version: 1,
  status: "created",
  created_at: "2026-07-19T00:01:00Z",
  updated_at: "2026-07-19T00:02:00Z",
};

describe("OpenAPI to editor view adapters", () => {
  it("joins books and projects without pretending the wire models are identical", () => {
    const [book] = adaptBooks([rawBook], [rawProject]);
    expect(book).toMatchObject({ id: "book-1", source_format: "epub", target_locales: ["zh-CN"], cover_policy: "preserve" });
    expect(book.section_count).toBeNull();
  });

  it("maps the backend session's intentionally smaller shape", () => {
    const session = adaptSession({
      owner: { email: "owner@example.com", identity_provider: "github-idp" },
      csrf_token: "signed-token",
      service: { status: "ready", database: "ready", provider_configured: false },
    });
    expect(session.owner.display_name).toBeNull();
    expect(session.service.version).toBeTruthy();
    expect(new Date(session.csrf_expires_at).getTime()).toBeGreaterThan(Date.now());
  });

  it("maps library rights and provider capability field names exactly", () => {
    const library = adaptLibrary({
      id: "lib", name: "reference", mode: "paired", target_locale: "zh-TW", priority: 100,
      rights_confirmed: true, allows_external_snippets: true, confirmed_at: "2026-07-19T00:00:00Z",
      notes: "private", status: "awaiting_review", profile: {}, source_upload_ids: ["a", "b"], pairings: [],
      alignment_review: [], created_at: "2026-07-19T00:00:00Z", updated_at: "2026-07-19T00:00:00Z",
    } satisfies RawLibrary);
    expect(library).toMatchObject({ source_count: 2, external_processing_allowed: true, alignment_state: "review" });

    const provider = adaptProvider({ configured: true, api_key_tail: "1234", capabilities: { models: true, json_response_format: false, streaming: "unprobed" } });
    expect(provider).toMatchObject({ key_suffix: "1234", capabilities: { models_endpoint: true, json_response_format: false, streaming: null } });
  });

  it("maps block_id and translation fields into stable editable segments", () => {
    const [segment] = adaptSegments({
      page: 1, page_size: 50, total: 1,
      items: [{
        block_id: "block-1", stable_id: "stable-1", section_id: "section-1", section_title: "第一章",
        kind: "paragraph", source_text: "原文", translation: "译文", revision_kind: "human", locked: true,
        context_version: 2,
        candidates: [{ id: "candidate-1", text: "重译候选", kind: "model_candidate", source: "retranslate", revision_no: 3, context_version: 2 }],
      }],
    }, "zh-CN");
    expect(segment).toMatchObject({ id: "block-1", target_text: "译文", locked: true, status: "reviewed" });
    expect(segment.revisions[0]).toMatchObject({ id: "candidate-1", kind: "candidate", text: "重译候选" });
  });

  it("returns a missing project binding instead of calling the segments endpoint", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(Response.json([]));
    await expect(api.segments("book-1", "zh-TW")).resolves.toEqual({ items: [], next_cursor: null, project_id: null });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    fetchMock.mockRestore();
  });

  it("understands the backend error envelope without reflecting arbitrary HTML", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(Response.json({ error: { code: "API_INCOMPATIBLE", message: "Not compatible", details: { private: true } } }, { status: 422 }));
    await expect(api.provider()).rejects.toMatchObject({ status: 422, code: "API_INCOMPATIBLE", message: "Not compatible" });
    fetchMock.mockRestore();

    const htmlMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(new Response("<h1>private upstream trace</h1>", { status: 502, headers: { "Content-Type": "text/html" } }));
    await expect(api.provider()).rejects.toMatchObject({ status: 502, code: "HTTP_502", message: "请求未完成，请稍后重试。" });
    htmlMock.mockRestore();
  });

  it("uploads replacement covers with the cover purpose and nullable book binding", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(Response.json({ id: "cover-upload" }, { status: 201 }))
      .mockResolvedValueOnce(Response.json({ accepted: true }))
      .mockResolvedValueOnce(Response.json({ upload: { id: "cover-upload" }, book_id: null }));
    const cover = new NodeFile([new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 1, 2, 3])], "cover.jpg", { type: "image/jpeg" }) as unknown as File;
    await expect(api.uploadBook(cover, undefined, "cover")).resolves.toEqual({ upload_id: "cover-upload", book_id: null });
    const createBody = JSON.parse(String(fetchMock.mock.calls[0][1]?.body)) as Record<string, unknown>;
    expect(createBody).toMatchObject({ purpose: "cover", media_type: "image/jpeg" });
    expect(new Headers(fetchMock.mock.calls[1][1]?.headers).get("Idempotency-Key")).toContain("cover-upload:0:");
    fetchMock.mockRestore();
  });

  it("binds a completed replacement cover when creating the project", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(Response.json(rawProject));
    await api.createProject("book-1", "縦書きの本", "zh-CN", "replace", "cover-upload");
    const body = JSON.parse(String(fetchMock.mock.calls[0][1]?.body)) as Record<string, unknown>;
    expect(body).toMatchObject({ cover_policy: "replace", replacement_cover_upload_id: "cover-upload" });
    fetchMock.mockRestore();
  });

  it("uses the resume action to confirm an awaiting-review job", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(Response.json({ id: "job-1" }));
    await api.jobAction("job-1", "resume");
    expect(String(fetchMock.mock.calls[0][0])).toBe("/api/jobs/job-1/resume");
    expect(fetchMock.mock.calls[0][1]?.method).toBe("POST");
    fetchMock.mockRestore();
  });
});
