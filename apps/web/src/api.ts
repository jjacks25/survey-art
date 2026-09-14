// Thin API client for the job-broker backend. When auth is enabled the Cognito
// access token is attached as a Bearer header (API Gateway's JWT authorizer
// validates it); locally (authDisabled) no token is sent.

export interface Job {
  jobId: string;
  address: string;
  county: string;
  status: string;
  createdAt: number;
  updatedAt: number;
  fileCount: number;
  error?: string | null;
  logs: string[];
  metadata?: Record<string, unknown> | null;
  location?: { lat: number; lon: number } | null;
}

export interface FileEntry {
  name: string;
  size: number;
  /** Inline URL — used for the PDF preview. */
  url: string;
  /** Same object, signed as an attachment so the browser saves it. */
  downloadUrl: string;
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

  getFiles(jobId: string) {
    return this.request<{ jobId: string; files: FileEntry[] }>(`/api/jobs/${jobId}/files`);
  }

  cancelJob(jobId: string) {
    return this.request<void>(`/api/jobs/${jobId}`, { method: "DELETE" });
  }
}
