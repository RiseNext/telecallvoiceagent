export default function Home() {
  return (
    <main className="mx-auto flex max-w-2xl flex-1 flex-col justify-center gap-4 p-8">
      <h1 className="text-2xl font-semibold tracking-tight">RiseNext Voice AI</h1>
      <p className="text-sm leading-6 text-zinc-600 dark:text-zinc-400">
        Dashboard scaffold. No product surfaces are implemented yet — see{" "}
        <code className="rounded bg-zinc-100 px-1 py-0.5 dark:bg-zinc-800">docs/ROADMAP.md</code>{" "}
        for the current phase and{" "}
        <code className="rounded bg-zinc-100 px-1 py-0.5 dark:bg-zinc-800">PRD.md</code> for what is
        being built.
      </p>
    </main>
  );
}
