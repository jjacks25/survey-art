import { useEffect, useMemo, useRef, useState } from "react";
import { useAuth } from "react-oidc-context";
import {
  ActionIcon,
  Box,
  Button,
  Center,
  Code,
  Container,
  Group,
  Loader,
  Modal,
  ScrollArea,
  Select,
  SimpleGrid,
  Stack,
  Table,
  Tabs,
  Text,
  TextInput,
  Title,
  Tooltip,
  Badge,
  UnstyledButton,
  Alert,
  Divider,
} from "@mantine/core";

import { AppConfig } from "./config";
import { ApiClient, FileEntry, Job, JobSummary } from "./api";

const TERMINAL = new Set(["COMPLETED", "FAILED"]);

// Keys must match COUNTY_SCRAPERS in src/survey_art/pipeline.py.
// Toggle `enabled` here to control what shows up in the dropdown.
const ALL_COUNTIES = [
  { value: "CO_weld", label: "Weld, CO", enabled: true },
  { value: "CO_denver", label: "Denver, CO", enabled: false },
  { value: "CO_arapahoe", label: "Arapahoe, CO", enabled: false },
  { value: "CO_jefferson", label: "Jefferson, CO", enabled: false },
];

const COUNTIES = ALL_COUNTIES.filter((c) => c.enabled);

function statusColor(status: string): string {
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

function fileIcon(name: string): string {
  const ext = name.split(".").pop()?.toLowerCase() ?? "";
  return FILE_ICONS[ext] ?? "📄";
}

function isPdf(name: string): boolean {
  return name.toLowerCase().endsWith(".pdf");
}

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

function formatWhen(unixSeconds: number): string {
  return new Date(unixSeconds * 1000).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
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

function MetadataView({ data }: { data: Record<string, unknown> }) {
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
function PropertyMap({ job }: { job: Job }) {
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

/** Address form + live job status + downloadable results. */
function Dashboard({
  config,
  token,
  onLogout,
}: {
  config: AppConfig;
  token: string | undefined;
  onLogout?: () => void;
}) {
  const api = useMemo(() => new ApiClient(config.apiBase, () => token), [config.apiBase, token]);
  const [mode, setMode] = useState<string>("account");
  const [address, setAddress] = useState("");
  const [account, setAccount] = useState("");
  const [county, setCounty] = useState<string | null>(COUNTIES[0]?.value ?? null);
  const [job, setJob] = useState<Job | null>(null);
  const [files, setFiles] = useState<FileEntry[]>([]);
  const [previewFile, setPreviewFile] = useState<FileEntry | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [history, setHistory] = useState<JobSummary[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const canSubmit = mode === "address" ? !!address.trim() : !!account.trim() && !!county;
  const searching = submitting || (!!job && !TERMINAL.has(job.status));

  // One entry per property: keep only the most-recent search for each doc
  // prefix (the property identifier — see worker.py's _doc_prefix()). Jobs
  // with no doc_prefix yet (still running, or failed before upload) fall
  // back to deduping by address instead. `history` is already most-recent-
  // first (see jobs.list_jobs()), so the first occurrence of a key wins.
  const propertyHistory = useMemo(() => {
    const seen = new Set<string>();
    const result: JobSummary[] = [];
    for (const h of history) {
      const key = h.docPrefix || h.address;
      if (seen.has(key)) continue;
      seen.add(key);
      result.push(h);
    }
    return result;
  }, [history]);

  // Step 3A.5 exception docs (weld_county.py writes them "exception_{reception}.pdf")
  // are ALTA-cited leads, not the directly-extracted set — split them out so a
  // surveyor sees the primary documents first, with the exceptions clearly labeled.
  const primaryFiles = useMemo(() => files.filter((f) => !f.name.startsWith("exception_")), [files]);
  const exceptionFiles = useMemo(() => files.filter((f) => f.name.startsWith("exception_")), [files]);

  // job.metadata.extracted_ids (overview.json, see id_extraction.py) lists every
  // record ID the ALTA cites; only reception_number entries are auto-fetchable, and
  // demo mode caps how many of those actually get downloaded. Comparing the two shows
  // "fetched X of Y" so a demo run doesn't read as though it found everything.
  const totalReceptionIds = useMemo(() => {
    const ids = job?.metadata?.extracted_ids;
    if (!Array.isArray(ids)) return null;
    return ids.filter((i) => (i as { id_type?: string })?.id_type === "reception_number").length;
  }, [job?.metadata]);

  function renderFileCard(f: FileEntry) {
    return (
      <Box key={f.name} style={{ position: "relative", minWidth: 0 }}>
        {/* Anchor, not a button: an <a download> inside the card's
            UnstyledButton would be invalid nested-interactive markup,
            so it sits alongside it and floats over the corner. */}
        <Tooltip label="Download" withArrow>
          <ActionIcon
            component="a"
            href={f.downloadUrl}
            download={f.name}
            variant="default"
            size="sm"
            aria-label={`Download ${f.name}`}
            style={{ position: "absolute", top: 4, right: 4, zIndex: 1 }}
          >
            <Text span size="xs">⤓</Text>
          </ActionIcon>
        </Tooltip>
        <UnstyledButton
          onClick={() =>
            isPdf(f.name) ? setPreviewFile(f) : window.open(f.url, "_blank")
          }
          p="sm"
          style={{
            borderRadius: 8,
            border: "1px solid var(--mantine-color-gray-3)",
            width: "100%",
            overflow: "hidden",
            boxSizing: "border-box",
          }}
        >
          <Stack align="center" gap={4} style={{ minWidth: 0, width: "100%" }}>
            {isPdf(f.name) ? (
              <div style={{ width: "100%", height: 90, overflow: "hidden", borderRadius: 4, pointerEvents: "none" }}>
                <iframe
                  src={`${f.url}#toolbar=0&view=FitH`}
                  title={f.name}
                  style={{ width: "400%", height: 360, border: "none", transform: "scale(0.25)", transformOrigin: "top left" }}
                />
              </div>
            ) : (
              <Text style={{ fontSize: 32 }}>{fileIcon(f.name)}</Text>
            )}
            <Text
              size="xs"
              ta="center"
              lineClamp={2}
              style={{ wordBreak: "break-word", width: "100%" }}
            >
              {f.name}
            </Text>
            <Text size="xs" c="dimmed">{(f.size / 1024).toFixed(1)} KB</Text>
          </Stack>
        </UnstyledButton>
      </Box>
    );
  }

  useEffect(() => {
    loadHistory();
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, []);

  async function submit() {
    if (!canSubmit) return;
    setError(null);
    setFiles([]);
    setSubmitting(true);
    try {
      const { jobId } =
        mode === "address"
          ? await api.createJob(address.trim())
          : await api.createJob(account.trim(), county!);
      if (pollRef.current) clearInterval(pollRef.current);
      pollRef.current = setInterval(() => poll(jobId), 1500);
      await poll(jobId);
      loadHistory();
    } catch (e) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  }

  function cancel() {
    if (pollRef.current) clearInterval(pollRef.current);
    setSubmitting(false);
    if (job) api.cancelJob(job.jobId).catch(() => {}); // job may have already finished server-side
    setJob(null);
    setError(null);
  }

  async function poll(jobId: string) {
    try {
      const j = await api.getJob(jobId);
      setJob(j);
      if (TERMINAL.has(j.status)) {
        if (pollRef.current) clearInterval(pollRef.current);
        if (j.status === "COMPLETED") {
          const { files } = await api.getFiles(jobId);
          setFiles(files);
        }
        loadHistory();
      }
    } catch (e) {
      setError(String(e));
      if (pollRef.current) clearInterval(pollRef.current);
    }
  }

  async function loadHistory() {
    setHistoryLoading(true);
    try {
      const { jobs } = await api.listJobs();
      setHistory(jobs);
    } catch {
      // history is a convenience — a failed fetch just leaves the list empty
    } finally {
      setHistoryLoading(false);
    }
  }

  /** Re-open a past search from the history sidebar: reuses the same
   * status-polling path as a fresh submit() so an in-progress job picked
   * from history keeps live-updating exactly like one just submitted. */
  function selectJob(jobId: string) {
    setError(null);
    setFiles([]);
    setSubmitting(false);
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(() => poll(jobId), 1500);
    poll(jobId);
  }

  return (
    <Group align="flex-start" gap={0} wrap="nowrap" style={{ minHeight: "100vh" }}>
      <Box
        w={280}
        miw={220}
        p="md"
        style={{
          borderRight: "1px solid var(--mantine-color-default-border)",
          height: "100vh",
          position: "sticky",
          top: 0,
          overflow: "hidden",
          display: "flex",
          flexDirection: "column",
        }}
      >
        <Group justify="space-between" mb="sm">
          <Text fw={700} size="sm">Search History</Text>
          <ActionIcon variant="subtle" onClick={loadHistory} aria-label="Refresh history">
            ↻
          </ActionIcon>
        </Group>
        {historyLoading ? (
          <Center py="lg"><Loader size="sm" /></Center>
        ) : propertyHistory.length === 0 ? (
          <Text c="dimmed" size="sm">No past searches yet.</Text>
        ) : (
          <ScrollArea style={{ flex: 1 }}>
            <Stack gap={4}>
              {propertyHistory.map((h) => (
                <UnstyledButton
                  key={h.docPrefix || h.jobId}
                  onClick={() => selectJob(h.jobId)}
                  p="xs"
                  style={{
                    borderRadius: 8,
                    width: "100%",
                    overflow: "hidden",
                    boxSizing: "border-box",
                    border:
                      job?.jobId === h.jobId
                        ? "1px solid var(--mantine-color-blue-5)"
                        : "1px solid transparent",
                  }}
                >
                  <Stack gap={2} style={{ minWidth: 0, width: "100%" }}>
                    <Text size="sm" fw={500} style={{ wordBreak: "break-word" }}>
                      {h.address}
                    </Text>
                    <Group gap="xs">
                      <Badge size="sm" color={statusColor(h.status)}>{h.status}</Badge>
                      <Text size="xs" c="dimmed">{formatWhen(h.createdAt)}</Text>
                    </Group>
                  </Stack>
                </UnstyledButton>
              ))}
            </Stack>
          </ScrollArea>
        )}
      </Box>

      <Container size="sm" py="xl" px="md" style={{ flex: 1, minWidth: 0 }}>
      <Group justify="space-between" mb="lg" wrap="nowrap">
        <Title order={2}>Survey Art</Title>
        {onLogout && <Button variant="subtle" onClick={onLogout}>Sign out</Button>}
      </Group>

      <Stack>
        <Tabs value={mode} onChange={(v) => v && setMode(v)}>
          <Tabs.List>
            <Tabs.Tab value="account">Account / Parcel #</Tabs.Tab>
            <Tabs.Tab value="address">Address</Tabs.Tab>
          </Tabs.List>

          <Tabs.Panel value="account" pt="sm">
            <Stack gap="sm">
              <Select
                label="County"
                placeholder="Select a county"
                data={COUNTIES}
                value={county}
                onChange={setCounty}
              />
              <TextInput
                label="Account / parcel number"
                placeholder="R1611986"
                value={account}
                onChange={(e) => setAccount(e.currentTarget.value)}
                onKeyDown={(e) => e.key === "Enter" && canSubmit && submit()}
              />
            </Stack>
          </Tabs.Panel>

          <Tabs.Panel value="address" pt="sm">
            <TextInput
              label="Property address"
              placeholder="123 Main St, Greeley, CO 80631"
              value={address}
              onChange={(e) => setAddress(e.currentTarget.value)}
              onKeyDown={(e) => e.key === "Enter" && canSubmit && submit()}
            />
          </Tabs.Panel>
        </Tabs>

        <Group grow>
          <Button onClick={submit} disabled={!canSubmit || searching}>
            {searching ? "Searching..." : "Search Records"}
          </Button>
          {searching && (
            <Button variant="outline" color="red" onClick={cancel}>
              Cancel
            </Button>
          )}
        </Group>

        {error && <Alert color="red" title="Error">{error}</Alert>}

        {job && (
          <Stack gap="xs">
            <Group>
              <Text fw={500}>Status:</Text>
              <Badge color={statusColor(job.status)}>{job.status}</Badge>
              {!TERMINAL.has(job.status) && <Loader size="xs" />}
            </Group>
            {job.error && <Alert color="red">{job.error}</Alert>}

            <Tabs defaultValue="logs">
              <Tabs.List>
                <Tabs.Tab value="logs">Logs</Tabs.Tab>
                <Tabs.Tab value="results">Results{files.length > 0 ? ` (${files.length})` : ""}</Tabs.Tab>
                <Tabs.Tab value="metadata">Property Metadata</Tabs.Tab>
                <Tabs.Tab value="map">Map</Tabs.Tab>
              </Tabs.List>

              <Tabs.Panel value="logs" pt="sm">
                {!job.logs || job.logs.length === 0 ? (
                  <Text c="dimmed" size="sm">No log output yet.</Text>
                ) : (
                  <Code
                    block
                    style={{
                      maxHeight: 320,
                      overflowY: "auto",
                      whiteSpace: "pre-wrap",
                      wordBreak: "break-word",
                    }}
                  >
                    {job.logs.join("\n")}
                  </Code>
                )}
              </Tabs.Panel>

              <Tabs.Panel value="results" pt="sm">
                {job.status !== "COMPLETED" ? (
                  <Text c="dimmed" size="sm">Results will appear here once the job completes.</Text>
                ) : files.length === 0 ? (
                  <Text c="dimmed" size="sm">No documents found.</Text>
                ) : (
                  <Stack gap="md">
                    <SimpleGrid cols={{ base: 2, sm: 3, md: 4 }} spacing="sm">
                      {primaryFiles.map((f) => renderFileCard(f))}
                    </SimpleGrid>
                    {exceptionFiles.length > 0 && (
                      <>
                        <Divider
                          label={
                            totalReceptionIds !== null
                              ? `ALTA-cited exceptions (fetched ${exceptionFiles.length} of ${totalReceptionIds})`
                              : `ALTA-cited exceptions (${exceptionFiles.length})`
                          }
                          labelPosition="left"
                        />
                        <SimpleGrid cols={{ base: 2, sm: 3, md: 4 }} spacing="sm">
                          {exceptionFiles.map((f) => renderFileCard(f))}
                        </SimpleGrid>
                      </>
                    )}
                  </Stack>
                )}
              </Tabs.Panel>

              <Tabs.Panel value="metadata" pt="sm">
                {job.metadata ? (
                  <MetadataView data={job.metadata} />
                ) : (
                  <Text c="dimmed" size="sm">
                    {job.status === "COMPLETED"
                      ? "No property metadata was collected for this county yet."
                      : "Metadata will appear here once the job completes."}
                  </Text>
                )}
              </Tabs.Panel>

              <Tabs.Panel value="map" pt="sm">
                <PropertyMap job={job} />
              </Tabs.Panel>
            </Tabs>
          </Stack>
        )}
      </Stack>

      <Modal
        opened={!!previewFile}
        onClose={() => setPreviewFile(null)}
        title={previewFile?.name}
        size="90%"
      >
        {previewFile && (
          <iframe
            src={previewFile.url}
            title={previewFile.name}
            style={{ width: "100%", height: "80vh", border: "none" }}
          />
        )}
      </Modal>
      </Container>
    </Group>
  );
}

/** Wraps the dashboard with Cognito sign-in when auth is enabled. */
function AuthedApp({ config }: { config: AppConfig }) {
  const auth = useAuth();

  if (auth.isLoading) {
    return <Center h="100vh"><Loader /></Center>;
  }
  if (auth.error) {
    return <Center h="100vh"><Alert color="red" title="Sign-in error">{auth.error.message}</Alert></Center>;
  }
  if (!auth.isAuthenticated) {
    return (
      <Center h="100vh">
        <Stack align="center">
          <Title order={2}>Survey Art</Title>
          <Text c="dimmed">Please sign in to continue.</Text>
          <Button onClick={() => auth.signinRedirect()}>Sign in</Button>
        </Stack>
      </Center>
    );
  }
  return (
    <Dashboard
      config={config}
      token={auth.user?.access_token}
      onLogout={() => auth.signoutRedirect()}
    />
  );
}

export function App({ config }: { config: AppConfig }) {
  return config.authDisabled ? (
    <Dashboard config={config} token={undefined} />
  ) : (
    <AuthedApp config={config} />
  );
}
