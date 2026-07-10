import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // "Recovery control center": near-black slate, one amber accent for attention.
        base: {
          950: "#08090c",
          900: "#0d0f14",
          850: "#12151c",
          800: "#171b24",
          700: "#232833",
          600: "#333a48",
          500: "#4c5468",
        },
        ink: {
          100: "#f2f4f8",
          200: "#d5dae4",
          300: "#a4adbe",
          400: "#7b8496",
        },
        status: {
          active: "#38bdf8",
          suspected: "#fbbf24",
          recoverable: "#f97316",
          claimed: "#a78bfa",
          completed: "#34d399",
          cancelled: "#94a3b8",
          dead: "#f43f5e",
        },
      },
      fontFamily: {
        sans: ["ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "Roboto", "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "monospace"],
      },
      boxShadow: {
        panel: "0 1px 0 0 rgba(255,255,255,0.04) inset, 0 12px 32px -12px rgba(0,0,0,0.7)",
      },
    },
  },
  plugins: [],
};

export default config;
