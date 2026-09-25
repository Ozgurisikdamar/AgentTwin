/** Identifier formats accepted in page URLs (anything else is a 404). */
export const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export const TRACE_ID = /^[0-9a-f]{32}$/i;
/** A scenario or dataset name (the evaluation API's `Name`). */
export const NAME = /^[a-z0-9][a-z0-9_-]{0,98}$/;
