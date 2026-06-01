import Image from "next/image";

import { cn } from "@/lib/utils";

/**
 * Corlinman wordmark. The glyph is the corlinman mascot (pixel-art, served
 * from /mascot.png with a transparent background). Used in sidebar + login +
 * palette.
 */
export function BrandMark({
  compact = false,
  className,
}: {
  compact?: boolean;
  className?: string;
}) {
  return (
    <div className={cn("flex items-center gap-2", className)}>
      <Image
        src="/mascot.png"
        alt="corlinman"
        width={30}
        height={30}
        priority
        className="h-[30px] w-[30px] shrink-0 object-contain"
        style={{ filter: "drop-shadow(0 1px 3px rgba(0,0,0,0.28))" }}
      />
      {compact ? null : (
        <div className="min-w-0 flex-col leading-tight">
          <div className="text-[14.5px] font-semibold tracking-[-0.015em] text-tp-ink">
            corlinman
          </div>
        </div>
      )}
    </div>
  );
}
