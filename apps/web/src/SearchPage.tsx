import { type KeyboardEvent, useState } from "react";
import { useNavigate, useOutletContext } from "react-router-dom";
import {
  Alert,
  Button,
  Checkbox,
  FileInput,
  Group,
  Loader,
  Select,
  Stack,
  Tabs,
  Text,
  TextInput,
} from "@mantine/core";

import type { KmzParcel } from "./api";
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
  const [kmzParcels, setKmzParcels] = useState<KmzParcel[]>([]);
  const [kmzSelected, setKmzSelected] = useState<string[]>([]);
  const [kmzParsing, setKmzParsing] = useState(false);
  const [kmzError, setKmzError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const canSubmit =
    mode === "address"
      ? !!address.trim()
      : mode === "kmz"
        ? (!!kmzAccount.trim() || kmzSelected.length > 0) && !!county
        : !!account.trim() && !!county;

  /** A KMZ export carries the county's own account/parcel number in its
   * ExtendedData — extracting it routes through the exact same Account/Parcel #
   * lookup as manual entry, so this only ever populates `kmzAccount` for the
   * user to review, it never submits on its own. A KMZ that's only a drawing (a
   * pipeline route, a sketched boundary) names no account; the API returns every
   * parcel the drawing touches instead, and each one checked becomes its own job. */
  async function handleKmzFile(file: File | null) {
    setKmzFile(file);
    setKmzAccount("");
    setKmzParcels([]);
    setKmzSelected([]);
    setKmzError(null);
    if (!file) return;
    setKmzParsing(true);
    try {
      const { identifier, parcels } = await api.identifyKmz(file);
      if (identifier) {
        setKmzAccount(identifier);
      } else if (parcels.length) {
        setKmzParcels(parcels);
        setKmzSelected(parcels.map((p) => p.account));
      } else {
        setKmzError("Couldn't find an account/parcel number or any parcels under this KMZ.");
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
      if (mode === "kmz" && !kmzAccount.trim()) {
        const jobs = [];
        for (const acct of kmzSelected) jobs.push(await api.createJob(acct, county!));
        loadHistory();
        navigate(`/jobs/${jobs[0].jobId}`);
        return;
      }
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

  const submitOnEnter = (e: KeyboardEvent) => {
    if (e.key === "Enter" && canSubmit) submit();
  };
  const countySelect = (
    <Select
      label="County"
      placeholder="Select a county"
      data={COUNTIES}
      value={county}
      onChange={setCounty}
    />
  );

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
            {countySelect}
            <TextInput
              label="Account / parcel number"
              placeholder="R1611986"
              value={account}
              onChange={(e) => setAccount(e.currentTarget.value)}
              onKeyDown={submitOnEnter}
            />
          </Stack>
        </Tabs.Panel>

        <Tabs.Panel value="address" pt="sm">
          <TextInput
            label="Property address"
            placeholder="123 Main St, Greeley, CO 80631"
            value={address}
            onChange={(e) => setAddress(e.currentTarget.value)}
            onKeyDown={submitOnEnter}
          />
        </Tabs.Panel>

        <Tabs.Panel value="kmz" pt="sm">
          <Stack gap="sm">
            {countySelect}
            <FileInput
              label="KMZ"
              description="A parcel exported from the county GIS site, or any drawn route or area"
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
                onKeyDown={submitOnEnter}
              />
            )}
            {kmzParcels.length > 0 && (
              <Checkbox.Group
                label={`${kmzParcels.length} parcel(s) under this drawing`}
                description="Each checked parcel is searched as its own job."
                value={kmzSelected}
                onChange={setKmzSelected}
              >
                <Stack gap={6} mt="xs">
                  {kmzParcels.map((p) => (
                    <Checkbox
                      key={p.account}
                      value={p.account}
                      label={`${p.account} — ${p.owner || "unknown owner"}`}
                      description={[p.situs, p.str_code && `S-T-R ${p.str_code}`]
                        .filter(Boolean)
                        .join(" · ")}
                    />
                  ))}
                </Stack>
              </Checkbox.Group>
            )}
          </Stack>
        </Tabs.Panel>
      </Tabs>

      <Button onClick={submit} disabled={!canSubmit || submitting}>
        {submitting
          ? "Starting..."
          : mode === "kmz" && !kmzAccount && kmzSelected.length > 1
            ? `Search ${kmzSelected.length} Parcels`
            : "Search Records"}
      </Button>

      {error && <Alert color="red" title="Error">{error}</Alert>}
    </Stack>
  );
}
