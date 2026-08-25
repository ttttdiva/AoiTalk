import { AnimatedGridPattern } from "@/components/magicui/animated-grid-pattern";

export default function AuthLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-background p-6">
      <AnimatedGridPattern
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 z-0 h-full w-full [mask-image:radial-gradient(ellipse_at_center,black_0%,transparent_72%)]"
        numSquares={24}
        maxOpacity={0.08}
        duration={4.5}
        repeatDelay={1.25}
      />
      <div className="relative z-10 w-full max-w-sm">
        {children}
      </div>
    </div>
  );
}
