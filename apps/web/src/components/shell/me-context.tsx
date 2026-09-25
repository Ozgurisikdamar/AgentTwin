"use client";

import { type ReactNode, createContext, useContext } from "react";
import type { Me } from "@/lib/types";

const MeContext = createContext<Me | null>(null);

export function MeProvider({ me, children }: { me: Me; children: ReactNode }) {
  return <MeContext.Provider value={me}>{children}</MeContext.Provider>;
}

/** The signed-in user (null outside the app shell, e.g. in isolated tests). */
export function useMe(): Me | null {
  return useContext(MeContext);
}

/**
 * Whether the signed-in user holds a permission (e.g. "simulation.run").
 * Only decides what the UI offers; the API enforces the same rules.
 */
export function useCan(permission: string): boolean {
  return useMe()?.permissions.includes(permission) ?? false;
}
