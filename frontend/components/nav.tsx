"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/", label: "Overview" },
  { href: "/workflows", label: "Workflows" },
  { href: "/recoverable", label: "Recovery queue" },
  { href: "/skill", label: "API & Skill" },
];

export function Nav() {
  const pathname = usePathname();

  return (
    <nav className="flex items-center gap-1" aria-label="Primary">
      {LINKS.map((link) => {
        const active =
          link.href === "/" ? pathname === "/" : pathname.startsWith(link.href);
        return (
          <Link
            key={link.href}
            href={link.href}
            aria-current={active ? "page" : undefined}
            className={`rounded-lg px-3 py-1.5 text-sm transition ${
              active
                ? "bg-base-800 text-ink-100"
                : "text-ink-400 hover:bg-base-850 hover:text-ink-200"
            }`}
          >
            {link.label}
          </Link>
        );
      })}
    </nav>
  );
}
