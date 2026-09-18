// Thin API client for the job-broker backend. When auth is enabled the Cognito
// access token is attached as a Bearer header (API Gateway's JWT authorizer
// validates it); locally (authDisabled) no token is sent.

// "milestone" = a plain-English progress step for a non-technical surveyor;
// "detail" = a verbose developer diagnostic. See survey_shared.jobs.LogEntry.
export interface LogEntry {
  message: string;
  kind: "milestone" | "detail";
}

export interface Job {
  jobId: string;
  address: string;
  county: string;
  status: string;
  createdAt: number;
  updatedAt: number;
  fileCount: number;
  error?: string | null;
  logs: LogEntry[];
  metadata?: Record<string, unknown> | null;
  location?: { lat: number; lon: number } | null;
  docPrefix?: string | null;
  bedrockCostUsd?: number | null;
  bedrockInputTokens?: number | null;
  bedrockOutputTokens?: number | null;
  fargateCostUsd?: number | null;
  fargateSeconds?: number | null;
}

export interface FileEntry {
  name: string;
  size: number;
  /** Inline URL — used for the PDF preview. */
  url: string;
  /** Same object, signed as an attachment so the browser saves it. */
  downloadUrl: string;
  /** Presigned URL for a small first-page JPEG, if the worker generated one —
   * absent for non-PDFs or a PDF with no embedded page raster to thumbnail. */
  thumbnailUrl?: string | null;
}

export interface JobSummary {
  jobId: string;
  address: string;
  county: string;
  status: string;
  createdAt: number;
  fileCount: number;
  docPrefix?: string | null;
}

/** A property that's been searched at least once, kept permanently — unlike
 * JobSummary/the sidebar's run history, this list survives deleteJob/deleteProperty. */
export interface SavedProperty {
  key: string;
  address: string;
  county: string;
  savedAt: number;
}

export class ApiClient {
  constructor(
    private apiBase: string,
    private getToken: () => string | undefined,
  ) {}

  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    const token = this.getToken();
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (token) headers.Authorization = `Bearer ${token}`;
    const resp = await fetch(`${this.apiBase}${path}`, { ...init, headers });
    if (!resp.ok) throw new Error(`${resp.status} ${await resp.text()}`);
    if (resp.status === 204) return undefined as T;
    return (await resp.json()) as T;
  }

  createJob(address: string, county?: string) {
    return this.request<{ jobId: string; status: string }>("/api/jobs", {
      method: "POST",
      body: JSON.stringify({ address, county: county || null }),
    });
  }

  getJob(jobId: string) {
    return this.request<Job>(`/api/jobs/${jobId}`);
  }

  listJobs() {
    return this.request<{ jobs: JobSummary[] }>("/api/jobs");
  }

  listSavedProperties() {
    return this.request<{ properties: SavedProperty[] }>("/api/saved-properties");
  }

  getFiles(jobId: string) {
    return this.request<{ jobId: string; files: FileEntry[] }>(`/api/jobs/${jobId}/files`);
  }

  cancelJob(jobId: string) {
    return this.request<void>(`/api/jobs/${jobId}`, { method: "DELETE" });
  }

  /** Same endpoint as cancelJob — the API cancels a running job or deletes its
   * record outright depending on whether it's still non-terminal. Kept as a
   * separate name so call sites (Cancel vs. Delete buttons) read clearly. */
  deleteJob(jobId: string) {
    return this.request<void>(`/api/jobs/${jobId}`, { method: "DELETE" });
  }

  /** Deletes every run recorded for one property (a job's docPrefix, or its
   * address if it has none yet), not just a single jobId — see
   * `jobs.property_key()` on the API side and Layout.tsx's propertyHistory
   * grouping, which this key must match. */
  deleteProperty(key: string) {
    return this.request<void>(`/api/properties?key=${encodeURIComponent(key)}`, { method: "DELETE" });
  }

  /** Upload a KMZ for account/parcel extraction. No Content-Type header —
   * the browser sets the multipart boundary itself when given a FormData body. */
  async identifyKmz(file: File) {
    const token = this.getToken();
    const headers: Record<string, string> = {};
    if (token) headers.Authorization = `Bearer ${token}`;
    const form = new FormData();
    form.append("file", file);
    const resp = await fetch(`${this.apiBase}/api/kmz/identify`, {
      method: "POST",
      headers,
      body: form,
    });
    if (!resp.ok) throw new Error(`${resp.status} ${await resp.text()}`);
    return (await resp.json()) as { identifier: string | null };
  }
}
