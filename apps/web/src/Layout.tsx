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
  const navigate = useNavigate();
  // Present only on /jobs/:jobId, undefined on the search page — used to
  // highlight the currently-open job in the sidebar.
  const { jobId } = useParams<{ jobId: string }>();

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
              {propertyHistory.map((h) => (
                <UnstyledButton
                  key={h.docPrefix || h.jobId}
                  onClick={() => navigate(`/jobs/${h.jobId}`)}
                  p="xs"
                  style={{
                    borderRadius: 8,
                    width: "100%",
                    overflow: "hidden",
                    boxSizing: "border-box",
                    border:
                      jobId === h.jobId
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
