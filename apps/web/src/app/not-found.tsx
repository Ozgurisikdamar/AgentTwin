import Link from "next/link";

export default function NotFound() {
  return (
    <main className="mx-auto max-w-xl p-8">
      <h1 className="text-lg font-semibold text-slate-900">Page not found</h1>
      <p className="mt-2 text-sm text-slate-700">
        The page does not exist. Go to{" "}
        <Link href="/overview" className="text-indigo-700 hover:underline">
          the overview
        </Link>
        .
      </p>
    </main>
  );
}
