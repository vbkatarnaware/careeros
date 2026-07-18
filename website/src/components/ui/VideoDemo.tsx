import { useEffect, useRef, useState } from "react";

/**
 * A silent, looping demo clip of the real product, framed like a terminal
 * card (consistent chrome with CommandBlock). Plays only while in view and
 * never autoplays under prefers-reduced-motion — the poster stands in instead.
 */
export default function VideoDemo({
  src,
  poster,
  label,
  caption,
}: {
  /** Basename in /media, without extension — e.g. "hero-pipeline". */
  src: string;
  poster?: string;
  label?: string;
  caption?: string;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [reducedMotion, setReducedMotion] = useState(false);

  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    setReducedMotion(mq.matches);
    const onChange = (e: MediaQueryListEvent) => setReducedMotion(e.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  useEffect(() => {
    const video = videoRef.current;
    if (!video || reducedMotion) return;

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          video.play().catch(() => {});
        } else {
          video.pause();
        }
      },
      { threshold: 0.35 },
    );
    observer.observe(video);
    return () => observer.disconnect();
  }, [reducedMotion]);

  const posterSrc = poster ? `/media/${poster}.jpg` : `/media/${src}.jpg`;

  return (
    <figure className="m-0">
      <div className="overflow-hidden rounded-xl border border-border bg-surface-2">
        {label && (
          <div className="flex items-center gap-2 border-b border-border px-4 py-2.5">
            <span className="flex gap-1.5" aria-hidden="true">
              <span className="h-2.5 w-2.5 rounded-full" style={{ background: "var(--border-strong)" }} />
              <span className="h-2.5 w-2.5 rounded-full" style={{ background: "var(--border-strong)" }} />
              <span className="h-2.5 w-2.5 rounded-full" style={{ background: "var(--border-strong)" }} />
            </span>
            <span className="font-mono text-xs text-faint">{label}</span>
          </div>
        )}
        <div className="relative bg-surface-2 p-3 sm:p-4">
          <div className="overflow-hidden rounded-lg border border-border">
            {reducedMotion ? (
              <img
                src={posterSrc}
                alt={caption ?? label ?? "CareerOS running"}
                className="block w-full"
              />
            ) : (
              <video
                ref={videoRef}
                muted
                loop
                playsInline
                preload="none"
                poster={posterSrc}
                className="block w-full"
                aria-label={caption ?? label ?? "CareerOS running"}
              >
                <source src={`/media/${src}.webm`} type="video/webm" />
                <source src={`/media/${src}.mp4`} type="video/mp4" />
              </video>
            )}
          </div>
        </div>
      </div>
      {caption && (
        <figcaption className="mt-3 text-sm leading-relaxed text-muted">
          {caption}
        </figcaption>
      )}
    </figure>
  );
}
