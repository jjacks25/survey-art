export const TERMINAL = new Set(["COMPLETED", "FAILED", "CANCELLED"]);

export function statusColor(status: string): string {
  if (status === "COMPLETED") return "green";
  if (status === "FAILED") return "red";
  if (status === "RUNNING") return "blue";
  return "gray";
}

const FILE_ICONS: Record<string, string> = {
  pdf: "📕",
  tif: "🖼️",
  tiff: "🖼️",
  jpg: "🖼️",
  jpeg: "🖼️",
  png: "🖼️",
  dwg: "📐",
  dxf: "📐",
  shp: "🗺️",
  kml: "🗺️",
  kmz: "🗺️",
  zip: "🗃️",
};

export function fileIcon(name: string): string {
  const ext = name.split(".").pop()?.toLowerCase() ?? "";
  return FILE_ICONS[ext] ?? "📄";
}

export function isPdf(name: string): boolean {
  return name.toLowerCase().endsWith(".pdf");
}

export function formatWhen(unixSeconds: number): string {
  return new Date(unixSeconds * 1000).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}
