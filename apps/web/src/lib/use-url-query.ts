"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useMemo, useOptimistic, useTransition } from "react";

/** `base` with `changes` applied: an empty value removes the key. */
export function withChanges(
  base: URLSearchParams | string,
  changes: Record<string, string>,
): URLSearchParams {
  const next = new URLSearchParams(base);
  for (const [k, v] of Object.entries(changes)) {
    if (v) next.set(k, v);
    else next.delete(k);
  }
  return next;
}

/**
 * Page state kept in the query string (shareable links, back/forward), read
 * optimistically.
 *
 * A control bound straight to useSearchParams keeps showing its old value
 * until the navigation commits, which takes a server round trip: a checkbox
 * looks like it ignored the click. Here the new query is shown at once and
 * handed over to the real one when the navigation lands.
 */
export function useUrlQuery(): {
  params: URLSearchParams;
  replace: (next: URLSearchParams) => void;
  update: (changes: Record<string, string>) => void;
} {
  const searchParams = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const [query, setQuery] = useOptimistic(searchParams.toString());
  const [, startTransition] = useTransition();
  const params = useMemo(() => new URLSearchParams(query), [query]);
  const replace = useCallback(
    (next: URLSearchParams) => {
      const qs = next.toString();
      startTransition(() => {
        setQuery(qs);
        router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
      });
    },
    [pathname, router, setQuery],
  );
  const update = useCallback(
    (changes: Record<string, string>) => replace(withChanges(query, changes)),
    [query, replace],
  );
  return { params, replace, update };
}
