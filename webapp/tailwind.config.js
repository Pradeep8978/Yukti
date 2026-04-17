// tailwind.config.js
/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      fontFamily: {
        sans:  ["'DM Sans'",     "sans-serif"],
        mono:  ["'DM Mono'",     "monospace"],
        display: ["'Syne'",      "sans-serif"],
      },
      colors: {
        brand: {
          50:  "#f0fdf4",
          100: "#dcfce7",
          400: "#4ade80",
          500: "#22c55e",
          600: "#16a34a",
          900: "#14532d",
        },
        surface: {
          0:   "#0a0d0f",
          1:   "#0f1417",
          2:   "#151b1f",
          3:   "#1c2429",
          4:   "#232d33",
          border: "#2a353d",
        },
        up:   "#22c55e",
        down: "#ef4444",
        warn: "#f59e0b",
        info: "#3b82f6",
      },
      animation: {
        "pulse-slow": "pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite",
        "fade-in":    "fadeIn 0.4s ease forwards",
        "slide-up":   "slideUp 0.35s ease forwards",
      },
      keyframes: {
        fadeIn:  { from: { opacity: "0" }, to: { opacity: "1" } },
        slideUp: { from: { opacity: "0", transform: "translateY(12px)" }, to: { opacity: "1", transform: "translateY(0)" } },
      },
    },
  },
  plugins: [],
};
