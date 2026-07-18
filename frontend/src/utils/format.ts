export function formatDate(value: string | null): string {
  if (!value) return "尚未记录";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(date);
}

export function localeName(value: string): string {
  if (value === "zh-CN") return "简体中文";
  if (value === "zh-TW") return "繁体中文（台湾）";
  return value;
}
