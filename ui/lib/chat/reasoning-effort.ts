export function modelSupportsReasoningEffort(model: string): boolean {
  const id = model.trim().toLowerCase();
  if (!id) return false;
  return (
    id.includes("codex") ||
    id === "o1" ||
    id === "o3" ||
    id === "o4" ||
    /^o[134](?:-|$)/.test(id) ||
    /(?:^|[/_-])gpt-5(?:[.-]|$)/.test(id)
  );
}
