"use client";

/**
 * /providers — redirect stub (PR4 model-hub consolidation).
 *
 * The providers admin surface moved to the canonical "Models & Keys" page
 * (`/models?tab=providers`, see `components/model-hub/`). This route stays
 * on disk so existing deep links and typed `<Link href="/providers">`
 * references keep compiling under `typedRoutes` + `output: "export"`.
 */

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTranslation } from "react-i18next";

export default function ProvidersPage() {
  const router = useRouter();
  const { t } = useTranslation();

  React.useEffect(() => {
    router.replace("/models?tab=providers");
  }, [router]);

  return (
    <p className="text-sm text-sg-ink-3">
      <Link
        href="/models?tab=providers"
        className="underline underline-offset-4 hover:text-sg-ink"
        data-testid="providers-moved-link"
      >
        {t("modelHub.moved")}
      </Link>
    </p>
  );
}
