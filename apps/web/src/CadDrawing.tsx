import { useEffect, useRef, useState } from "react";
import { Alert, Badge, Button, Group, Loader, Spoiler, Stack, Table, Text } from "@mantine/core";

import { ApiClient, FileEntry, Job } from "./api";
import { TERMINAL, fileIcon, statusColor } from "./utils";

/** One plotted description in the drawing's QC report — `cad_export.qc_rows()`. */
interface QcDescription {
  label: string;
  shape: string;
  status: "ok" | "check" | "info" | "failed";
  warnings: string[];
  note: string;
  georeferenced: boolean;
  rotationDeg: number;
  courses: number;
  misclosureFt: number | null;
  precision: number | null;
  areaAcres: number | null;
  calledAcres: number | null;
  suspects: { course: number; change: string; misclosureAfterFt: number }[];
}

interface QcDocument {
  reception: string;
  role: "subject" | "exception" | "easement";
  title: string;
  error: string | null;
  notes: string;
  descriptions: QcDescription[];
}

const STATUS_COLOR: Record<QcDescription["status"], string> = {
  ok: "green",
  check: "yellow",
  info: "gray",
  failed: "red",
};

const ROLE_LABEL: Record<QcDocument["role"], string> = {
  subject: "Vesting deed",
  exception: "Exception",
  easement: "Easement",
};

/** Download order: the drawing, then what a surveyor opens next. */
function fileRank(name: string): number {
  return [".dxf", "_points.csv", "_qc_report.csv"].findIndex((ext) => name.endsWith(ext)) >>> 0;
}

/** The CAD Drawing tab: a button that starts a drawing job for this search,
 * that job's progress, and once it's done the downloads plus the QC table.
 * The drawing job is a separate job record (`kind: "drawing"`), polled here
 * the same way ResultsPage polls the search. */
export function CadDrawing({ api, search }: { api: ApiClient; search: Job }) {
  const [drawingId, setDrawingId] = useState<string | null>(search.drawingJobId ?? null);
  const [drawing, setDrawing] = useState<Job | null>(null);
  const [files, setFiles] = useState<FileEntry[]>([]);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => setDrawingId(search.drawingJobId ?? null), [search.jobId, search.drawingJobId]);

  useEffect(() => {
    setDrawing(null);
    setFiles([]);
    if (pollRef.current) clearInterval(pollRef.current);
    if (!drawingId) return;
    const poll = async () => {
      try {
        const j = await api.getJob(drawingId);
        setDrawing(j);
        if (TERMINAL.has(j.status)) {
          if (pollRef.current) clearInterval(pollRef.current);
          if (j.status === "COMPLETED") setFiles((await api.getFiles(drawingId)).files);
        }
      } catch (e) {
        setError(String(e));
        if (pollRef.current) clearInterval(pollRef.current);
      }
    };
    pollRef.current = setInterval(poll, 3000);
    poll();
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [api, drawingId]);

  function start() {
    setStarting(true);
    setError(null);
    api
      .createDrawing(search.jobId)
      .then(({ jobId }) => setDrawingId(jobId))
      .catch((e) => setError(String(e)))
      .finally(() => setStarting(false));
  }

  if (search.status !== "COMPLETED") {
    return (
      <Text c="dimmed" size="sm">
        A CAD drawing can be made once the search has finished.
      </Text>
    );
  }

  const running = drawing !== null && !TERMINAL.has(drawing.status);
  const milestones = (drawing?.logs ?? []).filter((l) => l.kind === "milestone");
  const documents = ((drawing?.metadata?.drawing as { documents?: QcDocument[] } | undefined)
    ?.documents ?? []) as QcDocument[];
  const drawn = documents.flatMap((d) =>
    d.descriptions
      .filter((p) => p.status !== "info")
      .map((p) => ({ doc: d, p }))
  );
  const notDrawn = documents.filter(
    (d) => d.descriptions.length === 0 || d.descriptions.every((p) => p.status === "info")
  );

  return (
    <Stack gap="md">
      <Group justify="space-between">
        <Text size="sm" c="dimmed" maw={640}>
          Reads the vesting deed, every exception and every easement this search found, and
          plots their legal descriptions on the BLM section corners (Colorado State Plane North,
          US survey feet). Opens in Civil 3D, Carlson or any CAD program that reads DXF.
        </Text>
        <Button onClick={start} loading={starting} disabled={running}>
          {drawingId ? "Redraw" : "Create CAD drawing"}
        </Button>
      </Group>
      {error && <Alert color="red">{error}</Alert>}

      {drawing && (
        <Group gap="xs">
          <Text fw={500} size="sm">Drawing:</Text>
          <Badge color={statusColor(drawing.status)}>{drawing.status}</Badge>
          {running && <Loader size="xs" />}
          {running && milestones.length > 0 && (
            <Text size="sm" c="dimmed">{milestones[milestones.length - 1].message}</Text>
          )}
        </Group>
      )}
      {drawing?.error && <Alert color="red">{drawing.error}</Alert>}

      {files.length > 0 && (
        <Group gap="xs">
          {files
            .filter((f) => f.name !== "qc.json")
            .sort((a, b) => fileRank(a.name) - fileRank(b.name))
            .map((f) => (
              <Button
                key={f.name}
                component="a"
                href={f.downloadUrl}
                download={f.name}
                variant="default"
                leftSection={<Text span>{fileIcon(f.name)}</Text>}
              >
                {f.name}
              </Button>
            ))}
        </Group>
      )}

      {drawing?.status === "COMPLETED" && (
        <>
          <Text fw={500} size="sm">QC report</Text>
          <div style={{ overflowX: "auto" }}>
            <Table striped verticalSpacing="xs" fz="sm">
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>Reception</Table.Th>
                  <Table.Th>Document</Table.Th>
                  <Table.Th>Description</Table.Th>
                  <Table.Th>Check</Table.Th>
                  <Table.Th>Area (ac)</Table.Th>
                  <Table.Th>Called (ac)</Table.Th>
                  <Table.Th>Closure</Table.Th>
                  <Table.Th>Issues</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {drawn.map(({ doc, p }, i) => (
                  <Table.Tr key={`${doc.reception}-${i}`}>
                    <Table.Td>{doc.reception}</Table.Td>
                    <Table.Td>
                      <Text size="sm">{ROLE_LABEL[doc.role]}</Text>
                      <Text size="xs" c="dimmed" lineClamp={2}>{doc.title}</Text>
                    </Table.Td>
                    <Table.Td>{p.label}</Table.Td>
                    <Table.Td>
                      <Badge color={STATUS_COLOR[p.status]} variant="light">{p.status}</Badge>
                    </Table.Td>
                    <Table.Td>{p.areaAcres ?? "—"}</Table.Td>
                    <Table.Td>{p.calledAcres ?? "—"}</Table.Td>
                    <Table.Td>
                      {p.misclosureFt == null
                        ? "—"
                        : `${p.misclosureFt.toFixed(2)}′${p.precision ? ` (1:${p.precision.toLocaleString()})` : ""}`}
                    </Table.Td>
                    <Table.Td>
                      {[...p.warnings, ...p.suspects.map((s) => `Course ${s.course}: ${s.change}?`)].map(
                        (w) => (
                          <Text key={w} size="xs">{w}</Text>
                        )
                      )}
                    </Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </div>
          {notDrawn.length > 0 && (
            <Spoiler
              maxHeight={0}
              showLabel={`Show ${notDrawn.length} document(s) with nothing to draw`}
              hideLabel="Hide"
            >
              <Table verticalSpacing={4} fz="xs">
                <Table.Tbody>
                  {notDrawn.map((d) => (
                    <Table.Tr key={d.reception}>
                      <Table.Td>{d.reception}</Table.Td>
                      <Table.Td>{ROLE_LABEL[d.role]}</Table.Td>
                      <Table.Td>
                        {d.error ||
                          d.descriptions.map((p) => p.note).join("; ") ||
                          d.notes ||
                          "No land description in this document."}
                      </Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            </Spoiler>
          )}
          <Text size="xs" c="dimmed">
            Plotted from the recorded documents, not a survey. Every bearing and distance was
            read from the deed images by a model and should be checked against the document
            before the drawing is relied on.
          </Text>
        </>
      )}
    </Stack>
  );
}
