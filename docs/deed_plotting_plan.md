# Deed Plotting — Plan

Goal: once a property's documents have been scraped, a **"Create CAD drawing"** button on
its results page turns the recorded deeds into a DXF drawing with a QC report. The
drawing plots the vesting deed, each exception and each easement. The QC report covers
closure, called-vs-computed area, and likely blunders.

Everything here comes from standard surveying practice (COGO, traverse closure, curve
geometry) and our own data model. No third-party product's code, prompts, UI, naming or
file formats are used.

---

## User flow

1. A user runs a normal search. The job completes and the documents land in S3 under
   `job.docPrefix`.
2. A **Create CAD drawing** button appears on `ResultsPage` (`/jobs/:jobId`), enabled
   once the job is `COMPLETED`.
3. Clicking it calls `POST /api/jobs/{jobId}/drawing`. The API creates a **drawing job**
   that points at the source job's `docPrefix` and enqueues it on the existing SQS queue.
   The dispatcher launches a Fargate task as usual.
4. The page polls that job the same way it polls a search: streamed narration logs, then
   status. When it finishes, a **CAD Drawing** tab shows the per-document QC table plus
   download links for the DXF, the point file and the QC report.

Why reuse the job pipeline instead of a synchronous API call: reading scanned deeds takes
minutes of Bedrock time. The job pipeline already gives us queueing, logs, cancellation,
cost tracking and the Fargate image with `pypdf`/Pillow/Bedrock wired up.

---

## Plumbing changes

| Where | Change |
|---|---|
| `survey_shared/jobs.py` | `Job.kind: Literal["search","drawing"] = "search"` and `Job.source_job_id: str \| None`. `list_jobs()` callers filter out `drawing`, so the sidebar's one-row-per-property dedupe is untouched. Also add a lookup for the latest drawing job per source job |
| `apps/api` | `POST /api/jobs/{id}/drawing` returns 409 unless the source job is `COMPLETED` with a `docPrefix`. `GET /api/jobs/{id}` gains `drawingJobId` |
| SQS message | `{"jobId", "kind": "drawing", "docPrefix"}`. The dispatcher passes the body through unchanged |
| `worker.py` | `run_job` branches on `kind`. Drawing jobs download the PDFs under `docPrefix`, run the plotting pipeline, and upload the outputs to `documents/{…}/drawing/`. They reuse `finish()` and cost fields as-is |
| `apps/web` | Button + CAD Drawing tab on `ResultsPage`, plus `api.createDrawing(jobId)` in `api.ts` |
| IAM | The worker task role needs `s3:GetObject` on `documents/*` (it only writes today). Check `backend.yaml` per [`infra/AGENTS.md`](../infra/AGENTS.md) |

---

## Plotting pipeline (`apps/worker/survey_art/`)

| Module | Job |
|---|---|
| `cogo.py` | Pure math, stdlib only, no I/O. Bearing parsing, unit conversion, traverse, curve solving, closure, area, perimeter, misclosure analysis |
| `deed_parse.py` | PDF → `LegalDescription[]` via Bedrock tool-use with a Pydantic schema. Uses the text layer when the PDF has one, otherwise page images. Reuses the rasterize/tile/client helpers in `id_extraction.py` |
| `cad_export.py` | `LegalDescription[]` → DXF via `ezdxf` (new dependency), plus a PNEZD point CSV and the QC report JSON |
| `deed_plot.py` | Orchestration: choose which documents to plot (by `doc_classify` category), parse, solve, export, narrate progress |

### Data model (our own)

```python
class Course(BaseModel):          # one call in a legal description
    role: Literal["boundary", "tie"]   # tie = commencement / reference to a monument
    reverse: bool = False              # tie measured from the monument back to the point
    kind: Literal["line", "curve"]
    bearing: str | None                # as written in the deed, e.g. "N 45°30'15\" E"
    angle_mode: Literal["bearing", "azimuth", "deflection", "interior"] = "bearing"
    distance: str | None               # as written, e.g. "10 chains 2 links"
    curve: CurveData | None            # radius, delta, arc, chord brg/dist, radial, tangent, turn
    monument: str | None
    passing: list[PassingCall] = []
    adjoiner: str | None
    source_text: str                   # verbatim span, for review / highlighting later

class LegalDescription(BaseModel):
    title: str
    courses: list[Course]
    called_area: str | None
    plss: str | None                   # e.g. "NW1/4 Sec 12 T5N R66W 6th PM"
    deed: DeedFacts                    # grantor, grantee, reception / book-page, date
```

The LLM's only job is **transcribing and structuring** what the deed says. All
geometry, unit conversion and math happen in `cogo.py`, so they're deterministic and
testable.

### `cogo.py` scope
- **Directions:** quadrant bearings, azimuth (north and south), deflection, angle
  right/left, interior angles.
- **Units:** ft, US survey ft, in, yd, m, Gunter's chain, rod/pole/perch, link, plus mixed
  units in one distance ("2 chains 15 links").
- **Curves:** solve from any sufficient set of radius / delta / arc / chord bearing /
  chord distance / radial or tangent bearing, plus turn direction. Infer tangency from the
  previous course. Delta > 180° is allowed. Start with the combinations seen in our
  ground-truth deeds and add others as real deeds need them.
- **Closure:** misclosure distance and bearing, precision ratio, area (coordinate method
  plus circular segments), perimeter.
- **Misclosure analysis:** when closure exceeds the tolerance, test single-field changes
  per course (one bearing component or the distance) and rank them by the closure and
  area agreement they produce. This is a standard blunder check. Also solve for one
  course that closes the figure.

### DXF layout
The target is **AutoCAD Civil 3D**, the industry standard for survey drafting. We output
DXF R2010 in US survey feet, which also opens in Carlson, TBC, BricsCAD and IntelliCAD.
Layer names follow the US National CAD Standard survey discipline (`V-` prefix). Confirm
the exact minor codes against NCS v6 when implementing:

| Layer | Contents |
|---|---|
| `V-PROP-LINE` | Subject (vesting deed) boundary |
| `V-PROP-LINE-EXCP` | Exception parcels (one block per reception number, which also goes in the label) |
| `V-PROP-ESMT` | Easements |
| `V-PROP-TIE` | Tie / commencement courses |
| `V-PROP-TEXT` | Bearing/distance labels, line and curve tables, area + closure blocks |
| `V-PROP-ADJN` | Adjoiner callouts |
| `V-NODE` | Monuments and POB/POC points |

Each source document gets its own block reference named by reception number, so a user
can toggle or move a whole document in Civil 3D.

The drawing carries bearing/distance labels on each course, line/curve tables for
labels that don't fit, a POB marker, and an area + closure block per description.
Coordinates are local (POB = 5000,5000) until phase 5 georeferences them.

---

## Status (2026-10-02)

Phases 0–4 are built, and georeferencing (originally phase 5) came along with them.
The editing UI (phase 6) is still deferred.

| Phase | Where | Result |
|---|---|---|
| 0. Ground truth | `apps/worker/tests/deed_ground_truth.json` | 12 documents from R1611986, read by hand from the scans: the aliquot vesting deed, 6 metes-and-bounds parcels (one with a curve, one exhibit with two parcels), a centerline easement with a closing tie, a "South 950 feet of the East 485 feet" deed, and 3 documents with nothing to plot (a sketch-only right-of-way, a sketch-only meter site, and a chattel mortgage that a citation mis-resolved to). Every closed parcel closes to under 0.13′ and matches its called acreage to within 0.001 ac, which checks the transcription and `cogo` against each other. **Needs a PLS spot-check.** |
| 1. COGO | `cogo.py` | Parsing (quadrant, azimuth and cardinal bearings; ft, chains, links, rods, varas, yards, metres), curve solving, closure, area with curve segments, single-course blunder ranking, centerline offsets |
| 2. Transcription | `deed_parse.py` | `python -m survey_art.deed_plot --eval`: **12/12** on the ground truth with Sonnet 4.6 (≈$0.06 a document) |
| 3. Drawing + QC | `deed_plot.py`, `cad_export.py`, `plss.py` | DXF R2010 (NCS `V-` layers, one block per reception number), PNEZD points, QC as CSV and JSON. Runs locally: `python -m survey_art.deed_plot <folder>` |
| 4. Plumbing | `Job.kind`, `POST /api/jobs/{id}/drawing`, `worker._draw`, `CadDrawing.tsx` | Covered by `test_end_to_end.py::test_a_cad_drawing_runs_over_a_finished_search` |
| 5. Georeference | `plss.py` | BLM PLSS CadNSDI section corners, served directly in State Plane North (EPSG:2231); bearings rotated by the deed's basis of bearings, or by the corners its courses run to |

On the full R1611986 property (63 documents read, ≈$4, ≈8 min) the drawing has 35
descriptions: 22 pass every check and 13 are flagged (misclosures, rotations too large to
trust, descriptions not tied to a PLSS corner). 95 more name only the area they lie in
("an easement across the SW1/4"), and those are listed rather than drawn.

### Measured choices
- **Pages are sent as full-width bands, not a grid.** The citation reader's 2×2 grid cuts
  every typed course line in half. On 1512031 the model then paired four courses with
  their neighbours' distances. Bands fixed it.
- **A cheap per-page filter (Haiku) picks which pages to read.** Reading in fixed page
  groups confused the model ("Exhibit A isn't in these images") and paid for signature
  pages.
- **Deed grammar is enforced in code, not the prompt.** The model sometimes started a
  reverse tie at the wrong corner, reversed a tie-out, or returned word-only strips as
  courses ("to the point of beginning"). `deed_parse._normalise` / `_complete` fix or
  drop these deterministically.
- **The eval scores the transcription, not the spelling.** Corners are compared by where
  they resolve, courses after parsing, and derived rectangles by shape.
- **Model:** Sonnet 5.5, Sonnet 5 and Opus 5 aren't enabled on this AWS account (Bedrock
  AccessDenied), so the default is `us.anthropic.claude-sonnet-4-6` (`DEED_PARSE_MODEL`).
  Re-run the eval when a newer model becomes available.

### Known gaps
- Exceptions aren't subtracted from the subject; each document is its own block.
- Strip easements' side lines aren't clipped to property lines.
- Government lots and irregular sections are subdivided as if regular.
- A single CRS (CO North). Other zones are needed once another county gets drawings.
- Descriptions tied to lot corners or roads (not PLSS) are drawn at a local origin and
  flagged.
- Run-to-run variation: a repeat eval occasionally flips one document (an end-corner
  choice). Run it twice before trusting a change.

### Skipped for now
Live AutoCAD plotting over COM (Windows-only; the DXF covers it), KML / ArcGIS traverse
export (easy to add when asked), and spiral curves.

---

## Decisions (2026-10-02)
1. **CAD target:** AutoCAD Civil 3D via DXF, with NCS-style `V-` layers (above).
2. **What gets plotted:** the vesting deed plus **every** exception and easement document
   the scrape found. There's no picker in v1.
3. **v1 bar:** DXF + QC report. Phase 6 (the editing UI) is deferred.
