import { PresenceOrb } from "@/components/ui/presence-orb";
import { cn } from "@/lib/utils";

/**
 * Corlinman wordmark — the eclipse pearl (the design language's signature
 * element) + the name in the display face. Static in chrome; only the
 * login/onboard hero pearl spins. Used in sidebar + login + palette.
 */
export function BrandMark({
  compact = false,
  className,
}: {
  compact?: boolean;
  className?: string;
}) {
  return (
    <div className={cn("flex items-center gap-2.5", className)}>
      <PresenceOrb size="md" className="!h-[26px] !w-[26px]" />
      {compact ? null : (
        <div className="min-w-0 flex-col leading-tight">
          <div className="font-display text-[14.5px] font-medium tracking-[0.01em] text-sg-ink">
            corlinman
          </div>
        </div>
      )}
    </div>
  );
}
