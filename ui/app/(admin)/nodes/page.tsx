"use client";

import * as React from "react";
import { motion } from "framer-motion";
import { useTranslation } from "react-i18next";

import { useMotion } from "@/components/ui/motion-safe";
import { NotImplementedBlock } from "@/components/nodes/offline-block";

/**
 * Distributed Nodes — Tidepool (Phase 5d).
 *
 * HONEST-ALIGN (R5): the runner-registry topology has NO backend. There is no
 * `GET /wstool/runners` route in the gateway, so the page used to poll an
 * always-empty mock (`fetchRunnersMock` → `[]`) every 5s and render the
 * generic "No runners registered" empty state — silently implying a working
 * but idle registry and an inert "Reconnect" affordance.
 *
 * Until a real endpoint ships we render a single truthful "not yet available"
 * panel and do NOT poll anything. The full topology / side-rail / detail-drawer
 * UI (and the mock summariser) are retained in `@/components/nodes/*` and
 * `@/lib/mocks/nodes` so they can be re-wired once the backend lands.
 *
 * See `audit/ARCH_DEBT.md` → "R5 — /nodes runner registry" for the real fix:
 * a gateway route exposing `corlinman-wstool`'s registry, then point a React
 * Query at `apiFetch("/v1/wstool/runners")` and implement reconnect.
 */

export default function NodesPage() {
  const { t } = useTranslation();
  const { reduced } = useMotion();

  return (
    <motion.div
      className="flex flex-col gap-5"
      initial={reduced ? undefined : { opacity: 0, y: 6 }}
      animate={reduced ? undefined : { opacity: 1, y: 0 }}
      transition={{ duration: 0.28, ease: [0.16, 1, 0.3, 1] }}
    >
      <header className="flex flex-col gap-3">
        <h1 className="font-sans text-[30px] font-semibold leading-[1.12] tracking-[-0.025em] text-sg-ink sm:text-[34px]">
          {t("nodes.tp.heroTitle")}
        </h1>
      </header>

      <NotImplementedBlock />
    </motion.div>
  );
}
