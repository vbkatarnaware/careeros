import { useState } from "react";

/**
 * A copyable command / code block. Lines starting with "$" render a dimmed
 * prompt marker; everything else is plain. Copying strips the prompt markers.
 */
export default function CommandBlock({
  code,
  label,
}: {
  code: string;
  label?: string;
}) {
  const [copied, setCopied] = useState(false);
  const lines = code.replace(/\n$/, "").split("\n");

  async function copy() {
    const toCopy = lines
      .map((l) => l.replace(/^\$\s?/, ""))
      .join("\n");
    try {
      await navigator.clipboard.writeText(toCopy);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch (e) {}
  }

  return (
    <div className="group relative overflow-hidden rounded-lg border border-border bg-surface-2">
      {label && (
        <div className="flex items-center justify-between border-b border-border px-4 py-2">
          <span className="font-mono text-xs text-faint">{label}</span>
        </div>
      )}
      <div className="relative">
        <pre className="overflow-x-auto px-4 py-3.5 font-mono text-[13px] leading-relaxed text-ink-2">
          <code>
            {lines.map((line, i) => {
              const isCmd = line.startsWith("$");
              return (
                <div key={i}>
                  {isCmd ? (
                    <>
                      <span className="select-none text-faint">$ </span>
                      <span className="text-ink">{line.slice(2)}</span>
                    </>
                  ) : line === "" ? (
                    " "
                  ) : (
                    <span>{line}</span>
                  )}
                </div>
              );
            })}
          </code>
        </pre>
        <button
          type="button"
          onClick={copy}
          aria-label="Copy to clipboard"
          className="absolute right-2.5 top-2.5 inline-flex h-8 w-8 items-center justify-center rounded-md border border-border bg-surface text-muted opacity-0 transition-all hover:text-ink focus-visible:opacity-100 group-hover:opacity-100"
        >
          {copied ? (
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="var(--good)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5" /></svg>
          ) : (
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" /></svg>
          )}
        </button>
      </div>
    </div>
  );
}
