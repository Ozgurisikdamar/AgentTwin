import { Fragment } from "react";

/**
 * The trace summary's tool sequence ("lookup_order>get_refund_policy>…").
 * It has no spaces, so a narrow column used to break it inside a tool name;
 * a <wbr> after each ">" lets it wrap between tools only. The text content
 * (what copy and search see) is unchanged.
 */
export function ToolSequence({ sketch }: { sketch: string }) {
  const steps = sketch.split(">");
  return (
    <code className="text-xs" data-testid="tool-sequence">
      {steps.map((step, i) => (
        <Fragment key={i}>
          {i > 0 ? (
            <>
              {">"}
              <wbr />
            </>
          ) : null}
          {step}
        </Fragment>
      ))}
    </code>
  );
}
