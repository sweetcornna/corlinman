import { StatusCardClient } from "./status-card-client";

export const dynamicParams = false;

export function generateStaticParams() {
  return [{ token: "__token__" }];
}

export default async function StatusPage({
  params,
}: {
  params: Promise<{ token?: string }>;
}) {
  const resolved = await params;
  return <StatusCardClient initialToken={resolved.token ?? "__token__"} />;
}
