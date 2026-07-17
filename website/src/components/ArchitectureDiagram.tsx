import { useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import type { Stage, StageKind } from "@/lib/pipeline";

const kindMeta: Record<StageKind, { label: string; dot: string; text: string }> = {
  deterministic: {
    label: "Deterministic",
    dot: "var(--muted)",
    text: "var(--muted)",
  },
  reasoning: {
    label: "AI reasoning",
    dot: "var(--accent)",
    text: "var(--accent)",
  },
  output: {
    label: "Output",
    dot: "var(--good)",
    text: "var(--good)",
  },
};

export default function ArchitectureDiagram({ stages }: { stages: Stage[] }) {
  const [active, setActive] = useState(0);
  const stage = stages[active];
  const meta = kindMeta[stage.kind];

  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,360px)_minmax(0,1fr)] lg:gap-10">
      {/* Flow column */}
      <ol className="relative flex flex-col" role="list">
        <span
          aria-hidden="true"
          className="absolute left-[19px] top-3 bottom-3 w-px"
          style={{ background: "var(--border)" }}
        />
        {stages.map((s, i) => {
          const m = kindMeta[s.kind];
          const isActive = i === active;
          return (
            <li key={s.id} className="relative">
              <button
                type="button"
                onMouseEnter={() => setActive(i)}
                onFocus={() => setActive(i)}
                onClick={() => setActive(i)}
                aria-pressed={isActive}
                className="group flex w-full items-center gap-4 rounded-lg px-2 py-2 text-left transition-colors"
                style={{ background: isActive ? "var(--surface-2)" : "transparent" }}
              >
                <span
                  className="relative z-10 flex h-10 w-10 shrink-0 items-center justify-center rounded-full border text-[11px] font-mono transition-all"
                  style={{
                    borderColor: isActive ? m.dot : "var(--border-strong)",
                    background: "var(--bg)",
                    color: isActive ? m.text : "var(--faint)",
                  }}
                >
                  <span
                    className="h-2 w-2 rounded-full transition-transform"
                    style={{
                      background: m.dot,
                      transform: isActive ? "scale(1.35)" : "scale(1)",
                    }}
                  />
                </span>
                <span className="min-w-0 flex-1">
                  <span
                    className="block text-[15px] font-medium transition-colors"
                    style={{ color: isActive ? "var(--ink)" : "var(--ink-2)" }}
                  >
                    {s.name}
                  </span>
                  <span className="block text-xs" style={{ color: m.text }}>
                    {m.label}
                  </span>
                </span>
                <svg
                  width="16"
                  height="16"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.8"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden="true"
                  className="shrink-0 transition-all"
                  style={{
                    color: "var(--faint)",
                    opacity: isActive ? 1 : 0,
                    transform: isActive ? "translateX(0)" : "translateX(-4px)",
                  }}
                >
                  <path d="M5 12h14M13 6l6 6-6 6" />
                </svg>
              </button>
            </li>
          );
        })}
      </ol>

      {/* Detail panel */}
      <div className="lg:sticky lg:top-24 lg:self-start">
        <div className="overflow-hidden rounded-xl border border-border bg-surface">
          <div className="border-b border-border px-6 py-5">
            <div className="flex items-center justify-between gap-3">
              <span
                className="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium"
                style={{ borderColor: meta.dot, color: meta.text }}
              >
                <span className="h-1.5 w-1.5 rounded-full" style={{ background: meta.dot }} />
                {meta.label}
              </span>
              <span className="font-mono text-xs text-faint">
                {String(active + 1).padStart(2, "0")} / {String(stages.length).padStart(2, "0")}
              </span>
            </div>
          </div>
          <div className="relative min-h-[280px] px-6 py-6">
            <AnimatePresence mode="wait">
              <motion.div
                key={stage.id}
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -6 }}
                transition={{ duration: 0.24, ease: [0.22, 1, 0.36, 1] }}
              >
                <h3 className="text-xl font-semibold tracking-tight text-ink">
                  {stage.name}
                </h3>
                <p className="mt-2 text-[15px] leading-relaxed text-ink-2">
                  {stage.purpose}
                </p>
                <dl className="mt-6 grid gap-x-6 gap-y-4 sm:grid-cols-2">
                  <Field label="Inputs" value={stage.inputs} />
                  <Field label="Outputs" value={stage.outputs} mono />
                </dl>
                <div className="mt-6 rounded-lg border border-border bg-surface-2 px-4 py-3.5">
                  <p className="eyebrow mb-1.5">Why it exists</p>
                  <p className="text-sm leading-relaxed text-ink-2">{stage.why}</p>
                </div>
              </motion.div>
            </AnimatePresence>
          </div>
        </div>
      </div>
    </div>
  );
}

function Field({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div>
      <dt className="eyebrow mb-1.5">{label}</dt>
      <dd
        className={`text-sm leading-relaxed text-ink-2 ${mono ? "font-mono text-[13px]" : ""}`}
      >
        {value}
      </dd>
    </div>
  );
}
