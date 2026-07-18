import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import {
  demoBooks,
  demoExports,
  demoJobs,
  demoLibraries,
  demoProvider,
  demoSegments,
  demoSession,
} from "../demo";
import type { Locale, ProviderUpdate } from "../types/api";

const withDemo = async <T,>(demoMode: boolean, demoValue: T, load: () => Promise<T>): Promise<T> =>
  demoMode ? demoValue : load();

export function useSession(demoMode: boolean) {
  return useQuery({
    queryKey: ["session", demoMode],
    queryFn: () => withDemo(demoMode, demoSession, api.session),
    staleTime: 4 * 60 * 1000,
    retry: false,
  });
}

export function useBooks(demoMode: boolean) {
  return useQuery({
    queryKey: ["books", demoMode],
    queryFn: () => withDemo(demoMode, { items: demoBooks, next_cursor: null }, api.books),
  });
}

export function useLibraries(demoMode: boolean) {
  return useQuery({
    queryKey: ["libraries", demoMode],
    queryFn: () => withDemo(demoMode, { items: demoLibraries, next_cursor: null }, api.libraries),
  });
}

export function useJobs(demoMode: boolean) {
  return useQuery({
    queryKey: ["jobs", demoMode],
    queryFn: () => withDemo(demoMode, { items: demoJobs, next_cursor: null }, api.jobs),
    refetchInterval: demoMode ? false : 10_000,
  });
}

export function useSegments(bookId: string | null, locale: Locale, demoMode: boolean) {
  return useQuery({
    queryKey: ["segments", bookId, locale, demoMode],
    queryFn: () =>
      withDemo(demoMode, { items: demoSegments, next_cursor: null, project_id: "demo-project" }, () => api.segments(bookId ?? "", locale)),
    enabled: bookId !== null,
  });
}

export function useProvider(demoMode: boolean) {
  return useQuery({
    queryKey: ["provider", demoMode],
    queryFn: () => withDemo(demoMode, demoProvider, api.provider),
  });
}

export function useExports(bookId: string | null, locale: Locale, demoMode: boolean) {
  return useQuery({
    queryKey: ["exports", bookId, locale, demoMode],
    queryFn: () => withDemo(demoMode, { items: demoExports, next_cursor: null }, () => api.exports(bookId ?? "", locale)),
    enabled: bookId !== null,
  });
}

export function useUpdateProvider(demoMode: boolean) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (value: ProviderUpdate) => {
      if (demoMode) return Promise.reject(new Error("示范模式不会保存设置。"));
      return api.updateProvider(value);
    },
    onSuccess: (value) => client.setQueryData(["provider", demoMode], value),
  });
}

export function useUpdateSegment(bookId: string, projectId: string | null, demoMode: boolean) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ segmentId, text, locked }: { segmentId: string; text: string; locked: boolean }) => {
      if (demoMode) return Promise.resolve();
      if (!projectId) return Promise.reject(new Error("请先建立译稿项目。"));
      return api.updateSegment(bookId, projectId, segmentId, text, locked).then(() => undefined);
    },
    onSuccess: () => client.invalidateQueries({ queryKey: ["segments", bookId] }),
  });
}

export function useCreateProject(bookId: string, bookTitle: string, locale: Locale, demoMode: boolean) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ cover, replacementCoverUploadId }: {
      cover: "preserve" | "replace" | "none";
      replacementCoverUploadId?: string;
    }) => {
      if (demoMode) return Promise.reject(new Error("示范模式不会创建项目。"));
      return api.createProject(bookId, bookTitle, locale, cover, replacementCoverUploadId);
    },
    onSuccess: async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: ["segments", bookId, locale] }),
        client.invalidateQueries({ queryKey: ["books"] }),
      ]);
    },
  });
}
