import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useOutletContext, useParams } from "react-router-dom";
import {
  ActionIcon,
  Alert,
  Badge,
  Box,
  Button,
  Center,
  Code,
  Divider,
  Group,
  Loader,
  Modal,
  SimpleGrid,
  Spoiler,
  Stack,
  Table,
  Tabs,
  Text,
  Timeline,
  Title,
  Tooltip,
  UnstyledButton,
} from "@mantine/core";

import { FileEntry, Job } from "./api";
import { LayoutContext } from "./Layout";
import { MetadataView, PropertyMap } from "./MetadataView";
import { TERMINAL, statusColor, fileIcon, isPdf } from "./utils";

function renderFileCard(f: FileEntry, onPreview: (f: FileEntry) => void) {
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
        onClick={() => (isPdf(f.name) ? onPreview(f) : window.open(f.url, "_blank"))}
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
          {f.thumbnailUrl ? (
            // A real, worker-generated first-page image — no PDF fetch/render
            // in the browser at all, so this is the fast path (see worker.py's
            // `_make_thumbnails()`). Falls back to the iframe below only when
            // no thumbnail was generated (e.g. a vector PDF, not a county scan).
            <img
              src={f.thumbnailUrl}
              alt={f.name}
              loading="lazy"
              style={{ width: "100%", height: 90, objectFit: "cover", borderRadius: 4 }}
            />
          ) : isPdf(f.name) ? (
            <div style={{ width: "100%", height: 90, overflow: "hidden", borderRadius: 4, pointerEvents: "none" }}>
              <iframe
                src={`${f.url}#toolbar=0&view=FitH`}
                title={f.name}
                loading="lazy"
                style={{ width: "150%", height: 135, border: "none", transform: "scale(0.667)", transformOrigin: "top left" }}
              />
            </div>
          ) : (
            <Text style={{ fontSize: 32 }}>{fileIcon(f.name)}</Text>
          )}
          <Text size="xs" ta="center" lineClamp={2} style={{ wordBreak: "break-word", width: "100%" }}>
            {f.name}
          </Text>
          <Text size="xs" c="dimmed">{(f.size / 1024).toFixed(1)} KB</Text>
        </Stack>
      </UnstyledButton>
    </Box>
  );
}

/** Estimated infra cost breakdown for one run. Bedrock is a real dollar figure
 * (browser-use's own usage accounting); Fargate is estimated from wall-clock
 * task runtime against a flat on-demand rate — see worker.py's `_cost_fields`. */
function RunCost({ job }: { job: Job }) {
  if (job.bedrockCostUsd == null && job.fargateCostUsd == null) {
    return (
      <Text c="dimmed" size="sm">
        Cost will appear here once the job finishes.
      </Text>
    );
  }
  const bedrock = job.bedrockCostUsd ?? 0;
  const fargate = job.fargateCostUsd ?? 0;
  const total = bedrock + fargate;
  const fmt = (n: number) => `$${n.toFixed(4)}`;
  return (
    <Stack gap="sm">
      <Text fw={500} size="sm">Estimated Cost</Text>
      <Table w="auto" verticalSpacing="xs">
        <Table.Tbody>
          <Table.Tr>
            <Table.Td>Bedrock / LLM</Table.Td>
            <Table.Td>{fmt(bedrock)}</Table.Td>
            <Table.Td c="dimmed">
              {(job.bedrockInputTokens ?? 0).toLocaleString()} in / {(job.bedrockOutputTokens ?? 0).toLocaleString()} out tokens
            </Table.Td>
          </Table.Tr>
          <Table.Tr>
            <Table.Td>ECS / Fargate</Table.Td>
            <Table.Td>{fmt(fargate)}</Table.Td>
            <Table.Td c="dimmed">{(job.fargateSeconds ?? 0).toFixed(0)}s runtime</Table.Td>
          </Table.Tr>
          <Table.Tr>
            <Table.Td fw={700}>Total</Table.Td>
            <Table.Td fw={700}>{fmt(total)}</Table.Td>
            <Table.Td />
          </Table.Tr>
        </Table.Tbody>
      </Table>
      <Text c="dimmed" size="xs">
        Fargate cost is estimated from task runtime, not billed usage. Bedrock cost is
        reported by the model provider's own usage accounting.
      </Text>
    </Stack>
  );
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(0)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}m ${s}s`;
}

/** How long a run took, and how that scales with what it found. `fargateSeconds`
 * (worker.py) is the task's own wall-clock time — the closest thing to "how long did
 * the scrape actually take" we have; createdAt->updatedAt also includes any time the
 * job sat PENDING waiting for a Fargate task to be dispatched. */
function RunTime({ job }: { job: Job }) {
  if (job.fargateSeconds == null) {
    return (
      <Text c="dimmed" size="sm">
        Run time will appear here once the job finishes.
      </Text>
    );
  }
  const taskSeconds = job.fargateSeconds;
  const totalSeconds = job.updatedAt - job.createdAt;
  const queueSeconds = Math.max(0, totalSeconds - taskSeconds);
  const fileCount = job.fileCount ?? 0;
  return (
    <Stack gap="sm">
      <Text fw={500} size="sm">Run Time</Text>
      <Table w="auto" verticalSpacing="xs">
        <Table.Tbody>
          <Table.Tr>
            <Table.Td>Total time (search → done)</Table.Td>
            <Table.Td>{formatDuration(totalSeconds)}</Table.Td>
          </Table.Tr>
          <Table.Tr>
            <Table.Td>Scraper run time</Table.Td>
            <Table.Td>{formatDuration(taskSeconds)}</Table.Td>
          </Table.Tr>
          {queueSeconds > 1 && (
            <Table.Tr>
              <Table.Td c="dimmed">Time waiting to start</Table.Td>
              <Table.Td c="dimmed">{formatDuration(queueSeconds)}</Table.Td>
            </Table.Tr>
          )}
          {fileCount > 0 && (
            <Table.Tr>
              <Table.Td>Average time per document</Table.Td>
              <Table.Td>{formatDuration(taskSeconds / fileCount)}</Table.Td>
            </Table.Tr>
          )}
        </Table.Tbody>
      </Table>
    </Stack>
  );
}

/** One job's live status + results — logs, files, metadata, map. Polls
 * GET /api/jobs/:jobId every 1.5s while the job is non-terminal; landing
 * here from the search page or from a history-panel click both just set
 * the jobId route param, so both paths hit the same poll loop. */
export function ResultsPage() {
  const { jobId } = useParams<{ jobId: string }>();
  const { api, loadHistory } = useOutletContext<LayoutContext>();
  const navigate = useNavigate();

  const [job, setJob] = useState<Job | null>(null);
  const [files, setFiles] = useState<FileEntry[]>([]);
  const [previewFile, setPreviewFile] = useState<FileEntry | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [deleteModalOpen, setDeleteModalOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [reprocessing, setReprocessing] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Schedule B-2/cross-reference exception docs (weld_county.py writes them
  // "exception_{reception}.pdf") are cited leads, not the directly-extracted set —
  // split them out so a surveyor sees the primary documents first, with the
  // exceptions clearly labeled.
  const primaryFiles = useMemo(() => files.filter((f) => !f.name.startsWith("exception_")), [files]);
  const exceptionFiles = useMemo(() => files.filter((f) => f.name.startsWith("exception_")), [files]);

  // job.logs mixes plain-English progress steps ("milestone") with verbose
  // developer diagnostics ("detail" — see survey_shared.jobs.LogEntry). The Logs
  // tab shows only the milestones by default so a non-technical surveyor gets a
  // clean step list, with the full raw feed available behind "Show technical log".
  const milestones = useMemo(
    () => (job?.logs ?? []).filter((l) => l.kind === "milestone").map((l) => l.message),
    [job?.logs]
  );

  // job.metadata.extracted_ids (overview.json, see id_extraction.py) lists every
  // record ID the ALTA cites; only reception_number entries are auto-fetchable, and
  // demo mode caps how many of those actually get downloaded. Comparing the two shows
  // "fetched X of Y" so a demo run doesn't read as though it found everything.
  const totalReceptionIds = useMemo(() => {
    const ids = job?.metadata?.extracted_ids;
    if (!Array.isArray(ids)) return null;
    return ids.filter((i) => (i as { id_type?: string })?.id_type === "reception_number").length;
  }, [job?.metadata]);

  async function poll(id: string) {
    try {
      const j = await api.getJob(id);
      setJob(j);
      if (TERMINAL.has(j.status)) {
        if (pollRef.current) clearInterval(pollRef.current);
        if (j.status === "COMPLETED") {
          const { files } = await api.getFiles(id);
          setFiles(files);
        }
        loadHistory();
      }
    } catch (e) {
      setError(String(e));
      if (pollRef.current) clearInterval(pollRef.current);
    }
  }

  // Re-poll from scratch whenever the route's jobId changes — covers both a
  // fresh submit() redirect and clicking a different entry in the history panel.
  useEffect(() => {
    setJob(null);
    setFiles([]);
    setError(null);
    if (pollRef.current) clearInterval(pollRef.current);
    if (!jobId) return;
    pollRef.current = setInterval(() => poll(jobId), 1500);
    poll(jobId);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId]);

  function cancel() {
    if (pollRef.current) clearInterval(pollRef.current);
    if (job) api.cancelJob(job.jobId).catch(() => {}); // job may have already finished server-side
    navigate("/");
  }

  function deleteRun() {
    if (!job) return;
    setDeleting(true);
    // Delete every run recorded for this property (see api.deleteProperty),
    // not just the one currently open — repeated searches for the same
    // property otherwise leave duplicate history entries behind.
    api
      .deleteProperty(job.docPrefix || job.address)
      .then(() => {
        loadHistory();
        navigate("/");
      })
      .catch((e) => {
        setDeleteModalOpen(false);
        setError(String(e));
      })
      .finally(() => setDeleting(false));
  }

  function reprocess() {
    if (!job) return;
    setReprocessing(true);
    api
      .createJob(job.address, job.county)
      .then(({ jobId }) => navigate(`/jobs/${jobId}`))
      .catch((e) => {
        setReprocessing(false);
        setError(String(e));
      });
  }

  if (error) {
    return <Alert color="red" title="Error">{error}</Alert>;
  }
  if (!job) {
    return <Center py="xl"><Loader /></Center>;
  }

  // identify_results (Weld only, for now — see worker.py's _doc_prefix()) has the
  // parcel number, resolved address, and S/T/R once the scrape resolves them.
  const identify = (job.metadata?.identify_results ?? {}) as Record<string, string>;
  const pageTitle = job.address;
  const subtitles = [
    identify.parcel_id && `Parcel Number: ${identify.parcel_id}`,
    identify.address && `Address: ${identify.address}`,
    identify.section_township_range && `Section/Township/Range: ${identify.section_township_range}`,
  ].filter(Boolean) as string[];

  return (
    <Stack gap="xs">
      <Stack gap={0}>
        <Title order={3}>{pageTitle}</Title>
        {subtitles.map((line) => (
          <Text key={line} c="dimmed" size="sm">{line}</Text>
        ))}
      </Stack>
      <Group justify="space-between" wrap="nowrap">
        <Group>
          <Text fw={500}>Status:</Text>
          <Badge color={statusColor(job.status)}>{job.status}</Badge>
          {!TERMINAL.has(job.status) && <Loader size="xs" />}
        </Group>
        {!TERMINAL.has(job.status) ? (
          <Button variant="outline" color="red" onClick={cancel}>
            Cancel
          </Button>
        ) : (
          <Group gap="xs">
            <Button variant="outline" onClick={reprocess} loading={reprocessing}>
              Reprocess
            </Button>
            <Button variant="outline" color="red" onClick={() => setDeleteModalOpen(true)}>
              Delete
            </Button>
          </Group>
        )}
      </Group>
      {job.error && <Alert color="red">{job.error}</Alert>}

      <Tabs defaultValue="logs">
        <Tabs.List>
          <Tabs.Tab value="logs">Logs</Tabs.Tab>
          <Tabs.Tab value="results">Results{files.length > 0 ? ` (${files.length})` : ""}</Tabs.Tab>
          <Tabs.Tab value="metadata">Property Metadata</Tabs.Tab>
          <Tabs.Tab value="map">Map</Tabs.Tab>
          <Tabs.Tab value="run-details">Run Details</Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value="logs" pt="sm">
          {!job.logs || job.logs.length === 0 ? (
            <Text c="dimmed" size="sm">No log output yet.</Text>
          ) : (
            <Stack gap="md">
              {milestones.length === 0 ? (
                <Text c="dimmed" size="sm">Getting started...</Text>
              ) : (
                <Timeline
                  active={milestones.length - (TERMINAL.has(job.status) ? 0 : 1)}
                  bulletSize={20}
                  lineWidth={2}
                >
                  {milestones.map((message, i) => (
                    <Timeline.Item
                      key={i}
                      bullet={
                        !TERMINAL.has(job.status) && i === milestones.length - 1 ? (
                          <Loader size={12} />
                        ) : undefined
                      }
                    >
                      <Text size="sm">{message}</Text>
                    </Timeline.Item>
                  ))}
                </Timeline>
              )}

              <Spoiler maxHeight={0} showLabel="Show technical log" hideLabel="Hide technical log">
                <Code
                  block
                  style={{
                    maxHeight: 320,
                    overflowY: "auto",
                    whiteSpace: "pre-wrap",
                    wordBreak: "break-word",
                  }}
                >
                  {job.logs.map((l) => l.message).join("\n")}
                </Code>
              </Spoiler>
            </Stack>
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
                {primaryFiles.map((f) => renderFileCard(f, setPreviewFile))}
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
                    {exceptionFiles.map((f) => renderFileCard(f, setPreviewFile))}
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

        <Tabs.Panel value="run-details" pt="sm">
          <Stack gap="xl">
            <RunCost job={job} />
            <RunTime job={job} />
          </Stack>
        </Tabs.Panel>
      </Tabs>

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

      <Modal opened={deleteModalOpen} onClose={() => setDeleteModalOpen(false)} title="Delete this property?" centered>
        <Stack gap="md">
          <Text size="sm">
            Removes every search run recorded for this property, not just this one — downloaded
            documents are kept.
          </Text>
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setDeleteModalOpen(false)} disabled={deleting}>
              Cancel
            </Button>
            <Button color="red" onClick={deleteRun} loading={deleting}>
              Delete
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
