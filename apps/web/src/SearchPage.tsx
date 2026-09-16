import { useState } from "react";
import { useNavigate, useOutletContext } from "react-router-dom";
import {
  Alert,
  Button,
  FileInput,
  Group,
  Loader,
  Select,
  Stack,
  Tabs,
  Text,
  TextInput,
} from "@mantine/core";

import { LayoutContext } from "./Layout";

// Keys must match COUNTY_SCRAPERS in src/survey_art/pipeline.py.
// Toggle `enabled` here to control what shows up in the dropdown.
const ALL_COUNTIES = [
  { value: "CO_weld", label: "Weld, CO", enabled: true },
  { value: "CO_denver", label: "Denver, CO", enabled: false },
  { value: "CO_arapahoe", label: "Arapahoe, CO", enabled: false },
  { value: "CO_jefferson", label: "Jefferson, CO", enabled: false },
];

const COUNTIES = ALL_COUNTIES.filter((c) => c.enabled);

/** The landing page: pick a search mode, fill it in, submit. Submitting
 * only creates the job and navigates to its results page (/jobs/:jobId) —
 * everything about watching a run lives there instead, so this page is
 * free to be revisited (via "New Search") to kick off another property
 * search while an earlier one keeps running server-side. */
export function SearchPage() {
  const { api, loadHistory } = useOutletContext<LayoutContext>();
  const navigate = useNavigate();

  const [mode, setMode] = useState<string>("account");
  const [address, setAddress] = useState("");
  const [account, setAccount] = useState("");
  const [county, setCounty] = useState<string | null>(COUNTIES[0]?.value ?? null);
  const [kmzFile, setKmzFile] = useState<File | null>(null);
  const [kmzAccount, setKmzAccount] = useState("");
  const [kmzParsing, setKmzParsing] = useState(false);
  const [kmzError, setKmzError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const canSubmit =
    mode === "address"
      ? !!address.trim()
      : mode === "kmz"
        ? !!kmzAccount.trim() && !!county
        : !!account.trim() && !!county;

  /** A KMZ export carries the county's own account/parcel number in its
   * ExtendedData — extracting it routes through the exact same Account/Parcel #
   * lookup as manual entry, so this only ever populates `kmzAccount` for the
   * user to review, it never submits on its own. */
  async function handleKmzFile(file: File | null) {
    setKmzFile(file);
    setKmzAccount("");
    setKmzError(null);
    if (!file) return;
    setKmzParsing(true);
    try {
      const { identifier } = await api.identifyKmz(file);
      if (identifier) {
        setKmzAccount(identifier);
      } else {
        setKmzError("Couldn't find an account/parcel number in this KMZ.");
      }
    } catch (e) {
      setKmzError(String(e));
    } finally {
      setKmzParsing(false);
    }
  }

  async function submit() {
    if (!canSubmit) return;
    setError(null);
    setSubmitting(true);
    try {
      const { jobId } =
        mode === "address"
          ? await api.createJob(address.trim())
          : await api.createJob((mode === "kmz" ? kmzAccount : account).trim(), county!);
      loadHistory();
      navigate(`/jobs/${jobId}`);
    } catch (e) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Stack>
      <Tabs value={mode} onChange={(v) => v && setMode(v)}>
        <Tabs.List>
          <Tabs.Tab value="account">Account / Parcel #</Tabs.Tab>
          <Tabs.Tab value="address">Address</Tabs.Tab>
          <Tabs.Tab value="kmz">KMZ</Tabs.Tab>
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

        <Tabs.Panel value="kmz" pt="sm">
          <Stack gap="sm">
            <Select
              label="County"
              placeholder="Select a county"
              data={COUNTIES}
              value={county}
              onChange={setCounty}
            />
            <FileInput
              label="Parcel KMZ"
              description="A KMZ exported from the county GIS site for a single parcel"
              placeholder="Upload a .kmz file"
              accept=".kmz"
              value={kmzFile}
              onChange={handleKmzFile}
              clearable
            />
            {kmzParsing && (
              <Group gap="xs">
                <Loader size="xs" />
                <Text size="sm" c="dimmed">Reading KMZ...</Text>
              </Group>
            )}
            {kmzError && <Alert color="red">{kmzError}</Alert>}
            {kmzAccount && (
              <TextInput
                label="Account / parcel number found"
                value={kmzAccount}
                onChange={(e) => setKmzAccount(e.currentTarget.value)}
                onKeyDown={(e) => e.key === "Enter" && canSubmit && submit()}
              />
            )}
          </Stack>
        </Tabs.Panel>
      </Tabs>

      <Button onClick={submit} disabled={!canSubmit || submitting}>
        {submitting ? "Starting..." : "Search Records"}
      </Button>

      {error && <Alert color="red" title="Error">{error}</Alert>}
    </Stack>
  );
}
