import type {
  BookSummary,
  ExportArtifact,
  ProviderStatus,
  ReferenceLibrary,
  Segment,
  Session,
  TranslationJob,
} from "./types/api";

export const demoSession: Session = {
  owner: {
    email: "zongrui0831@outlook.com",
    display_name: "Zongrui",
    identity_provider: "github",
  },
  csrf_token: "demo-only-token",
  csrf_expires_at: "2099-01-01T00:00:00Z",
  service: { status: "ready", version: "local-demo" },
};

export const demoBooks: BookSummary[] = [
  {
    id: "demo-vertical-epub",
    title: "試作短編集 — 縦書き検証版",
    author: "合成测试资料",
    source_format: "epub",
    source_language: "ja",
    target_locales: ["zh-CN", "zh-TW"],
    cover_policy: "preserve",
    state: "review",
    section_count: 3,
    updated_at: "2026-07-19T02:10:00+08:00",
    is_demo: true,
  },
  {
    id: "demo-text-layer-pdf",
    title: "町角の喫茶店",
    author: "合成测试资料",
    source_format: "pdf",
    source_language: "ja",
    target_locales: ["zh-CN"],
    cover_policy: "none",
    state: "ready",
    section_count: 2,
    updated_at: "2026-07-18T22:44:00+08:00",
    is_demo: true,
  },
];

export const demoLibraries: ReferenceLibrary[] = [
  {
    id: "demo-makeine-style",
    name: "败犬女主太多了 · 既有中文译本",
    mode: "style_only",
    target_locale: "zh-CN",
    priority: 100,
    source_count: 9,
    alignment_state: "not_applicable",
    external_processing_allowed: false,
    rights_confirmed_at: null,
    note: "等待同卷无 DRM 日文原版后升级为配对资料库。",
    is_demo: true,
  },
];

export const demoJobs: TranslationJob[] = [
  {
    id: "demo-job-cn",
    book_id: "demo-vertical-epub",
    book_title: "試作短編集 — 縦書き検証版",
    locale: "zh-CN",
    state: "awaiting_review",
    current_section: "第二章　雨の図書室",
    completed_blocks: 3,
    total_blocks: 3,
    issue_count: 1,
    updated_at: "2026-07-19T02:10:00+08:00",
    is_demo: true,
  },
];

export const demoSegments: Segment[] = [
  {
    id: "blk_demo_001",
    section_id: "sec_demo_02",
    section_title: "第二章　雨の図書室",
    ordinal: 1,
    source_text: "雨は昼すぎから細くなり、図書室の窓に銀色の筋を残していた。",
    target_text: "午后的雨渐渐细了，只在图书室的窗上留下银色的水痕。",
    target_locale: "zh-CN",
    locked: true,
    status: "reviewed",
    warnings: [],
    terminology: [{ source: "図書室", target: "图书室" }],
    revisions: [
      { id: "r1", kind: "model_draft", text: "雨从午后开始变小，在图书室的窗户上留下了银色的痕迹。", created_at: "2026-07-19T01:40:00+08:00" },
      { id: "r2", kind: "human", text: "午后的雨渐渐细了，只在图书室的窗上留下银色的水痕。", created_at: "2026-07-19T02:00:00+08:00" },
    ],
  },
  {
    id: "blk_demo_002",
    section_id: "sec_demo_02",
    section_title: "第二章　雨の図書室",
    ordinal: 2,
    source_text: "「まだ帰らないの？」栞が本棚の向こうから顔を出す。",
    target_text: "“你还不回去吗？”栞从书架后探出脸来。",
    target_locale: "zh-CN",
    locked: false,
    status: "warning",
    warnings: ["人名“栞”的地区用字尚未确认。"],
    terminology: [{ source: "栞", target: "栞", note: "角色名，待锁定" }],
    revisions: [
      { id: "r3", kind: "reviewed", text: "“还不回去吗？”栞从书架后探出头来。", created_at: "2026-07-19T01:47:00+08:00" },
      { id: "r4", kind: "candidate", text: "“你还不走吗？”栞从书架另一头露出脸来。", created_at: "2026-07-19T01:48:00+08:00" },
    ],
  },
  {
    id: "blk_demo_003",
    section_id: "sec_demo_02",
    section_title: "第二章　雨の図書室",
    ordinal: 3,
    source_text: "返事の代わりに、僕は読みかけの頁へ指を挟んだ。",
    target_text: "我没有回答，只把手指夹进读到一半的书页。",
    target_locale: "zh-CN",
    locked: false,
    status: "translated",
    warnings: [],
    terminology: [],
    revisions: [],
  },
];

export const demoProvider: ProviderStatus = {
  configured: false,
  base_url: null,
  key_suffix: null,
  translation_model: null,
  review_model: null,
  last_verified_at: null,
  capabilities: { models_endpoint: null, json_response_format: null, streaming: null },
};

export const demoExports: ExportArtifact[] = [
  {
    id: "demo-export",
    book_id: "demo-vertical-epub",
    locale: "zh-CN",
    format: "epub",
    status: "ready",
    sha256: "demo",
    created_at: "2026-07-19T02:04:00+08:00",
    download_url: null,
    validation_message: "示范条目，不提供下载。",
  },
];
