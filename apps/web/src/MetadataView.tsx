import { Button, Stack, Table, Text } from "@mantine/core";

import { Job } from "./api";

function formatCell(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

// Sections that exist in metadata for completeness (every field the county
// site returned, undeduplicated) but aren't meant for the curated view a
// surveyor reads — see the scraper's overview.json comment for why they're
// still collected.
const RAW_SECTION_KEYS = new Set(["raw_report_fields", "raw_property_report_fields"]);

const ACRONYMS = new Set(["url", "id", "sop", "pdf"]);

function titleCase(key: string): string {
  return key
    .replace(/_/g, " ")
    .split(" ")
    .filter(Boolean)
    .map((w) => (ACRONYMS.has(w.toLowerCase()) ? w.toUpperCase() : w[0].toUpperCase() + w.slice(1)))
    .join(" ");
}

function isUrl(value: unknown): value is string {
  return typeof value === "string" && /^https?:\/\//i.test(value);
}

/** Renders a metadata cell: a clickable link for URLs, plain text otherwise. */
function CellValue({ value }: { value: unknown }) {
  if (isUrl(value)) {
    return (
      <Text
        component="a"
        href={value}
        target="_blank"
        rel="noopener noreferrer"
        size="sm"
        style={{ wordBreak: "break-all" }}
      >
        {value}
      </Text>
    );
  }
  return (
    <Text span size="sm" style={{ wordBreak: "break-word", whiteSpace: "pre-wrap" }}>
      {formatCell(value)}
    </Text>
  );
}

// Related fields that should always sit next to each other, wherever they
// happen to fall in the source data (e.g. buried among dozens of unrelated
// property-report fields in DOM order).
const RELATED_FIELD_GROUPS: string[][] = [["section", "township", "range", "range_"]];

function groupRelatedEntries(entries: [string, unknown][]): [string, unknown][] {
  const keyOf = ([k]: [string, unknown]) => k.toLowerCase();
  for (const group of RELATED_FIELD_GROUPS) {
    const members = entries.filter((e) => group.includes(keyOf(e)));
    if (members.length < 2) continue;
    const firstIndex = entries.findIndex((e) => group.includes(keyOf(e)));
    const rest = entries.filter((e) => !group.includes(keyOf(e)));
    entries = [...rest.slice(0, firstIndex), ...members, ...rest.slice(firstIndex)];
  }
  return entries;
}

/** Renders one overview.json value: a list of records as a table, a flat
 * object as a key/value table, anything else as plain text. */
function MetadataValue({ value }: { value: unknown }) {
  if (Array.isArray(value)) {
    if (value.length === 0) return <Text size="sm" c="dimmed">None</Text>;
    if (typeof value[0] === "object" && value[0] !== null) {
      const keys = Object.keys(value[0] as Record<string, unknown>);
      return (
        <div style={{ overflowX: "auto", maxWidth: "100%" }}>
          <Table striped withTableBorder>
            <Table.Thead>
              <Table.Tr>{keys.map((k) => <Table.Th key={k} style={{ whiteSpace: "nowrap" }}>{titleCase(k)}</Table.Th>)}</Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {value.map((row, i) => (
                <Table.Tr key={i}>
                  {keys.map((k) => (
                    <Table.Td key={k} style={{ minWidth: 120 }}><CellValue value={(row as Record<string, unknown>)[k]} /></Table.Td>
                  ))}
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </div>
      );
    }
    return (
      <Stack gap={2}>
        {(value as unknown[]).map((v, i) => <CellValue key={i} value={v} />)}
      </Stack>
    );
  }
  if (value !== null && typeof value === "object") {
    const entries = groupRelatedEntries(Object.entries(value as Record<string, unknown>));
    if (entries.length === 0) return <Text size="sm" c="dimmed">None</Text>;
    return (
      <div style={{ overflowX: "auto", maxWidth: "100%" }}>
        <Table withTableBorder>
          <Table.Tbody>
            {entries.map(([k, v]) => (
              <Table.Tr key={k}>
                <Table.Td style={{ fontWeight: 500, whiteSpace: "nowrap", verticalAlign: "top" }}>{titleCase(k)}</Table.Td>
                <Table.Td style={{ minWidth: 160 }}><CellValue value={v} /></Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </div>
    );
  }
  return <Text size="sm"><CellValue value={value} /></Text>;
}

export function MetadataView({ data }: { data: Record<string, unknown> }) {
  return (
    <Stack gap="md">
      {Object.entries(data)
        .filter(([section]) => !RAW_SECTION_KEYS.has(section))
        .map(([section, value]) => (
        <div key={section}>
          <Text fw={600} mb={4}>{titleCase(section)}</Text>
          <MetadataValue value={value} />
        </div>
      ))}
    </Stack>
  );
}

/** Shows the actual property, not just a generic area:
 * 1. Weld's live parcel map (maps.weld.gov) — an ESRI map with the parcel
 *    boundary highlighted in red, captured in overview.json by the scraper.
 * 2. Otherwise a Google Maps embed pinned + zoomed to the geocoded lat/lon.
 * 3. Last resort: a plain address search (no precise pin). */
export function PropertyMap({ job }: { job: Job }) {
  const mapSection = job.metadata?.map as Record<string, unknown> | undefined;
  const boundaryImageUrl = typeof mapSection?.image_url === "string" ? mapSection.image_url : null;
  const liveMapUrl = typeof mapSection?.iframe_url === "string" ? mapSection.iframe_url : null;
  const loc = job.location;

  if (boundaryImageUrl) {
    return (
      <Stack gap="xs">
        <Text size="sm" c="dimmed">Property boundary (highlighted in red):</Text>
        <img
          src={boundaryImageUrl}
          alt="Property boundary map"
          style={{ width: "100%", borderRadius: 8, display: "block" }}
        />
        {liveMapUrl && (
          <Button
            component="a"
            href={liveMapUrl}
            target="_blank"
            rel="noopener noreferrer"
            variant="light"
            size="sm"
            style={{ alignSelf: "flex-start" }}
          >
            View on County Site
          </Button>
        )}
      </Stack>
    );
  }
  if (loc) {
    return (
      <iframe
        title="Property location"
        src={`https://www.google.com/maps?q=${loc.lat},${loc.lon}&z=19&output=embed`}
        style={{ width: "100%", height: 450, border: "none", borderRadius: 8 }}
        loading="lazy"
      />
    );
  }
  if (job.address) {
    return (
      <iframe
        title="Property location"
        src={`https://www.google.com/maps?q=${encodeURIComponent(job.address)}&output=embed`}
        style={{ width: "100%", height: 400, border: "none", borderRadius: 8 }}
        loading="lazy"
      />
    );
  }
  return <Text c="dimmed" size="sm">No location to show yet.</Text>;
}
