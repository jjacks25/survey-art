import { useEffect, useMemo, useState } from "react";
import { Link, Outlet, useNavigate, useParams } from "react-router-dom";
import {
  ActionIcon,
  Box,
  Button,
  Center,
  Container,
  Group,
  Loader,
  ScrollArea,
  Stack,
  Text,
  Title,
  Badge,
  UnstyledButton,
} from "@mantine/core";

import { AppConfig } from "./config";
import { ApiClient, JobSummary } from "./api";
import { statusColor, formatWhen } from "./utils";

export type LayoutContext = { api: ApiClient; loadHistory: () => void };

/** Shell shared by the search page and every job's results page: a
 * permanent search-history sidebar (so a running search stays one click
 * away while you start or review another) plus the page title/sign-out
 * row. The routed page renders via <Outlet>, reading `api`/`loadHistory`
 * from context instead of each page building its own ApiClient. */
export function Layout({
  config,
  token,
  onLogout,
}: {
  config: AppConfig;
  token: string | undefined;
  onLogout?: () => void;
}) {
  const api = useMemo(() => new ApiClient(config.apiBase, () => token), [config.apiBase, token]);
  const [history, setHistory] = useState<JobSummary[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [deletingRunId, setDeletingRunId] = useState<string | null>(null);
  const navigate = useNavigate();
  // Present only on /jobs/:jobId, undefined on the search page — used to
  // highlight the currently-open job in the sidebar.
  const { jobId } = useParams<{ jobId: string }>();

  // Group runs by property (docPrefix, falling back to address for jobs
  // with no docPrefix yet — still running, or failed before upload; see
  // survey_shared.jobs.property_key(), which the API's property-level
  // delete uses the same key for). Each group's `runs` is oldest-first for
  // the expanded view; `latest` (the first entry, since `history` itself is
  // most-recent-first — see jobs.list_jobs()) is what the collapsed row shows.
  const propertyHistory = useMemo(() => {
    const groups = new Map<string, JobSummary[]>();
    for (const h of history) {
      const key = h.docPrefix || h.address;
      const runs = groups.get(key);
      if (runs) runs.push(h);
      else groups.set(key, [h]);
    }
    return Array.from(groups.entries()).map(([key, runsMostRecentFirst]) => ({
      key,
      latest: runsMostRecentFirst[0],
      runs: [...runsMostRecentFirst].reverse(),
    }));
  }, [history]);

  function toggleExpanded(key: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  // Deletes one run outright (not the whole property — see ResultsPage's
  // "Delete" button for that). `api.deleteJob` cancels a non-terminal job or
  // deletes a terminal one's record; these are always past runs, so it's
  // always a delete here.
  function deleteRun(runJobId: string) {
    setDeletingRunId(runJobId);
    api
      .deleteJob(runJobId)
      .then(() => {
        loadHistory();
        if (jobId === runJobId) navigate("/");
      })
      .finally(() => setDeletingRunId(null));
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

  useEffect(() => {
    loadHistory();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
              {propertyHistory.map(({ key, latest, runs }) => {
                const isExpanded = expanded.has(key);
                const isOpenRun = runs.some((r) => r.jobId === jobId);
                return (
                  <Box key={key}>
                    <Group gap={2} wrap="nowrap" align="stretch">
                      <UnstyledButton
                        onClick={() => navigate(`/jobs/${latest.jobId}`)}
                        p="xs"
                        style={{
                          borderRadius: 8,
                          flex: 1,
                          minWidth: 0,
                          overflow: "hidden",
                          boxSizing: "border-box",
                          border: isOpenRun
                            ? "1px solid var(--mantine-color-blue-5)"
                            : "1px solid transparent",
                        }}
                      >
                        <Stack gap={2} style={{ minWidth: 0, width: "100%" }}>
                          <Text size="sm" fw={500} style={{ wordBreak: "break-word" }}>
                            {latest.address}
                          </Text>
                          <Group gap="xs">
                            <Badge size="sm" color={statusColor(latest.status)}>{latest.status}</Badge>
                            <Text size="xs" c="dimmed">{formatWhen(latest.createdAt)}</Text>
                            {runs.length > 1 && (
                              <Text size="xs" c="dimmed">({runs.length} runs)</Text>
                            )}
                          </Group>
                        </Stack>
                      </UnstyledButton>
                      {runs.length > 1 && (
                        <ActionIcon
                          variant="subtle"
                          onClick={() => toggleExpanded(key)}
                          aria-label={isExpanded ? "Collapse runs" : "Expand runs"}
                        >
                          {isExpanded ? "▾" : "▸"}
                        </ActionIcon>
                      )}
                    </Group>
                    {isExpanded && runs.length > 1 && (
                      <Stack gap={2} pl="md" mt={2}>
                        {runs.map((r) => (
                          <Group key={r.jobId} gap={2} wrap="nowrap" align="stretch">
                            <UnstyledButton
                              onClick={() => navigate(`/jobs/${r.jobId}`)}
                              p="xs"
                              style={{
                                borderRadius: 8,
                                flex: 1,
                                minWidth: 0,
                                overflow: "hidden",
                                boxSizing: "border-box",
                                border:
                                  jobId === r.jobId
                                    ? "1px solid var(--mantine-color-blue-5)"
                                    : "1px solid transparent",
                              }}
                            >
                              <Group gap="xs">
                                <Badge size="sm" color={statusColor(r.status)}>{r.status}</Badge>
                                <Text size="xs" c="dimmed">{formatWhen(r.createdAt)}</Text>
                              </Group>
                            </UnstyledButton>
                            <ActionIcon
                              variant="subtle"
                              color="red"
                              size="sm"
                              loading={deletingRunId === r.jobId}
                              onClick={() => deleteRun(r.jobId)}
                              aria-label="Delete this run"
                            >
                              ×
                            </ActionIcon>
                          </Group>
                        ))}
                      </Stack>
                    )}
                  </Box>
                );
              })}
            </Stack>
          </ScrollArea>
        )}
      </Box>

      <Container size="sm" py="xl" px="md" style={{ flex: 1, minWidth: 0 }}>
        <Group justify="space-between" mb="lg" wrap="nowrap">
          <Title order={2}>Survey Art</Title>
          <Group gap="sm" wrap="nowrap">
            <Button component={Link} to="/" variant="light">New Search</Button>
            {onLogout && <Button variant="subtle" onClick={onLogout}>Sign out</Button>}
          </Group>
        </Group>

        <Outlet context={{ api, loadHistory } satisfies LayoutContext} />
      </Container>
    </Group>
  );
}
