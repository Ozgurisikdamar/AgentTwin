export const dynamic = "force-dynamic";

/** Liveness of the web server itself (does not call the control plane). */
export function GET(): Response {
  return Response.json({ status: "ok" }, { headers: { "Cache-Control": "no-store" } });
}
