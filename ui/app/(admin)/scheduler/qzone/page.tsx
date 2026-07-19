"use client";

/**
 * /admin/scheduler/qzone — legacy route. The QZone publishing surface
 * now lives INSIDE the QQ channel page (it borrows the running NapCat
 * login state, so it belongs with the channel). This stub preserves old
 * bookmarks/deep-links by forwarding them there.
 */

import { useEffect } from "react";
import { useRouter } from "next/navigation";

export default function QzoneSchedulerRedirect() {
  const router = useRouter();
  useEffect(() => {
    router.replace("/channels/qq");
  }, [router]);
  return null;
}
