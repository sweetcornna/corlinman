"use client";

/**
 * /credentials — redirect stub (PR4 model-hub consolidation).
 *
 * The credentials manager (OAuth panel + advanced per-provider fields)
 * moved to the canonical "Models & Keys" page (`/models`, see
 * `components/model-hub/`). This route stays on disk so existing deep
 * links and typed `<Link href="/credentials">` references keep compiling
 * under `typedRoutes` + `output: "export"`.
 */

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTranslation } from "react-i18next";

export default function CredentialsPage() {
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
        data-testid="credentials-moved-link"
      >
        {t("modelHub.moved")}
      </Link>
    </p>
  );
}
