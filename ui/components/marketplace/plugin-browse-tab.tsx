"use client";

/**
 * `<PluginBrowseTab>` — Browse grid of plugin market items. Installs are
 * staged (plugin lands disabled); lifecycle lives on the Installed tab.
 */

import * as React from "react";
import { useQuery } from "@tanstack/react-query";

import {
  listPluginMarket,
  type PluginMarketItem,
  type PluginMarketResponse,
} from "@/lib/api";
import { MarketBrowseGrid } from "./market-browse-grid";
import { MarketCard } from "./market-card";
import { PluginDetailDrawer } from "./plugin-detail-drawer";

export function PluginBrowseTab(): React.JSX.Element {
  const [selected, setSelected] = React.useState<PluginMarketItem | null>(null);

  const query = useQuery<PluginMarketResponse>({
    queryKey: ["plugin-market", "list"],
    queryFn: () => listPluginMarket(),
    retry: false,
  });

  const rows = query.data?.rows ?? [];
  const offline = query.data?.offline === true || query.isError;

  const handleRetry = React.useCallback(() => {
    void query.refetch();
  }, [query]);

  return (
    <>
      <MarketBrowseGrid
        testId="plugin-browse"
        rows={rows}
        offline={offline}
        pending={query.isPending}
        onRetry={handleRetry}
        renderCard={(item) => <MarketCard item={item} onSelect={setSelected} />}
      />
      <PluginDetailDrawer
        item={selected}
        open={selected !== null}
        onOpenChange={(next) => {
          if (!next) setSelected(null);
        }}
      />
    </>
  );
}

export default PluginBrowseTab;
