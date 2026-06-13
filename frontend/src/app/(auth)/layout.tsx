export default function AuthLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-background p-6">
      <div
        className="absolute inset-0 bg-cover bg-center"
        style={{ backgroundImage: "url('/images/ui/login-crystal.png')" }}
      />
      <div className="absolute inset-0 bg-[linear-gradient(90deg,rgba(247,252,255,0.92)_0%,rgba(247,252,255,0.74)_44%,rgba(247,252,255,0.38)_100%)] dark:bg-[linear-gradient(90deg,rgba(6,13,24,0.94)_0%,rgba(8,18,31,0.78)_46%,rgba(9,24,40,0.52)_100%)]" />
      <div className="relative z-10 w-full max-w-sm">
        {children}
      </div>
    </div>
  );
}
