import { cn } from "@/lib/utils";

function Skeleton({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("shimmer rounded-sg-sm", className)}
      {...props}
    />
  );
}

export { Skeleton };
