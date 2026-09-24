"use client";

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <main className="mx-auto max-w-xl p-8" role="alert">
      <h1 className="text-lg font-semibold text-slate-900">Something went wrong</h1>
      <p className="mt-2 text-sm text-slate-700">
        The page failed to render{error.digest ? ` (reference ${error.digest})` : ""}.
      </p>
      <button
        type="button"
        onClick={reset}
        className="mt-4 rounded-md border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-50"
      >
        Try again
      </button>
    </main>
  );
}
