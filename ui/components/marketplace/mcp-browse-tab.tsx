"use client";

/**
 * `<McpBrowseTab>` — Browse grid of MCP market items. Installs are staged
 * (server lands disabled); lifecycle management lives on the Installed tab.
 */

import * as React from "react";
import { useQuery } from "@tanstack/react-query";

import {
  listMcpMarket,
  type McpMarketItem,
  type McpMarketResponse,
} from "@/lib/api";
import { MarketBrowseGrid } from "./market-browse-grid";
import { MarketCard } from "./market-card";
import { McpDetailDrawer } from "./mcp-detail-drawer";

export function McpBrowseTab(): React.JSX.Element {
  const [selected, setSelected] = React.useState<McpMarketItem | null>(null);

  const query = useQuery<McpMarketResponse>({
    queryKey: ["mcp-market", "list"],
    queryFn: () => listMcpMarket(),
    retry: false,
  });

  const rows = query.data?.rows ?? [];
  // `offline` is signalled in-band by the response, or by a hard query error.
  const offline = query.data?.offline === true || query.isError;

  const handleRetry = React.useCallback(() => {
    void query.refetch();
  }, [query]);

  return (
    <>
      <MarketBrowseGrid
        testId="mcp-browse"
        rows={rows}
        offline={offline}
        pending={query.isPending}
        onRetry={handleRetry}
        renderCard={(item) => (
          <MarketCard item={item} onSelect={setSelected} showTransport />
        )}
      />
      <McpDetailDrawer
        item={selected}
        open={selected !== null}
        onOpenChange={(next) => {
          if (!next) setSelected(null);
        }}
      />
    </>
  );
}

export default McpBrowseTab;
