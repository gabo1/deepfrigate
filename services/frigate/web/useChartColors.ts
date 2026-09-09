/**
 * Obsidiana Táctica — colores para ApexCharts.
 *
 * ApexCharts writes colors into SVG presentation attributes, where CSS
 * `var(--x)` is not valid, so the hex values live here. This is the ONLY
 * place besides themes/theme-default.css allowed to carry a color literal.
 * Keep both files in sync (color.chart.* of the design tokens).
 */
import { useMemo } from "react";
import { useTheme } from "@/context/theme-provider";

export type ChartColors = {
  grid: string;
  axis: string;
  tooltipBg: string;
  tooltipBorder: string;
  tooltipLabel: string;
  bar: string;
  cursor: string;
  dots: string;
  edge: string;
  /** Series palette: accent first, then info and warn. Never more than 3. */
  series: string[];
  crit: string;
  warn: string;
  ok: string;
  info: string;
  accent: string;
};

const DARK: ChartColors = {
  grid: "#1F242B",
  axis: "#697079",
  tooltipBg: "#15181D",
  tooltipBorder: "#2E353E",
  tooltipLabel: "#A4ACB7",
  bar: "#3D4650",
  cursor: "#1B2026",
  dots: "#1B2026",
  edge: "#1F242B",
  series: ["#3867FC", "#5B9DD9", "#E2A33C"],
  crit: "#E5484D",
  warn: "#E2A33C",
  ok: "#3FB57E",
  info: "#5B9DD9",
  accent: "#3867FC",
};

const LIGHT: ChartColors = {
  grid: "#E3E6EB",
  axis: "#6E7683",
  tooltipBg: "#FFFFFF",
  tooltipBorder: "#D2D7DE",
  tooltipLabel: "#454D57",
  bar: "#B9C0C9",
  cursor: "#EBEDF1",
  dots: "#D2D7DE",
  edge: "#D2D7DE",
  series: ["#335EEB", "#2C6FB0", "#B0741A"],
  crit: "#D02B31",
  warn: "#B0741A",
  ok: "#1A8A5C",
  info: "#2C6FB0",
  accent: "#335EEB",
};

export function useChartColors(): ChartColors {
  const { theme, systemTheme } = useTheme();
  const resolved = (systemTheme || theme) === "dark" ? "dark" : "light";
  return useMemo(() => (resolved === "dark" ? DARK : LIGHT), [resolved]);
}
