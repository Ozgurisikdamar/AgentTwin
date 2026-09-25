"use client";

import { useCallback, useState } from "react";
import { ApiError, newIdempotencyKey } from "./api";

/**
 * An Idempotency-Key for one user action. The key survives failures that
 * leave the outcome unknown (network error, 5xx), so retrying cannot repeat
 * the action; it is replaced after a definite answer (success or 4xx), so
 * the next action - even with the same body - is new.
 */
export function useActionKey(prefix: string): { key: string; settle: (error: unknown) => void } {
  const [key, setKey] = useState(() => newIdempotencyKey(prefix));
  const settle = useCallback(
    (error: unknown) => {
      const unknownOutcome =
        error instanceof ApiError ? error.status === 0 || error.status >= 500 : Boolean(error);
      if (!unknownOutcome) setKey(newIdempotencyKey(prefix));
    },
    [prefix],
  );
  return { key, settle };
}
