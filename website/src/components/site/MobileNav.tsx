import { useState } from "react";

interface NavItem {
  label: string;
  href: string;
}

export default function MobileNav({
  items,
  github,
}: {
  items: NavItem[];
  github: string;
}) {
  const [open, setOpen] = useState(false);

  return (
    <div className="md:hidden">
      <button
        type="button"
        aria-label={open ? "Close menu" : "Open menu"}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="inline-flex h-9 w-9 items-center justify-center rounded-md border border-border text-ink"
      >
        {open ? (
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" /></svg>
        ) : (
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true"><path d="M3 6h18M3 12h18M3 18h18" /></svg>
        )}
      </button>

      {open && (
        <div className="fixed inset-x-0 top-16 z-50 border-b border-border bg-bg/95 backdrop-blur">
          <nav className="container-page flex flex-col gap-1 py-4">
            {items.map((item) => (
              <a
                key={item.href}
                href={item.href}
                className="rounded-md px-3 py-2.5 text-[15px] text-ink-2 transition-colors hover:bg-surface-2 hover:text-ink"
                onClick={() => setOpen(false)}
              >
                {item.label}
              </a>
            ))}
            <a
              href={github}
              target="_blank"
              rel="noopener noreferrer"
              className="mt-1 rounded-md px-3 py-2.5 text-[15px] text-ink-2 transition-colors hover:bg-surface-2 hover:text-ink"
            >
              GitHub ↗
            </a>
          </nav>
        </div>
      )}
    </div>
  );
}
