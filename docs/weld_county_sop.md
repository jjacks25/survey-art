# Weld County SOP — ALTA / Exemption / Easement Extraction

This document captures the human-validated standard operating procedure for collecting
land-survey records from Weld County, Colorado. It is the source-of-truth specification
that the Weld County scraper ([`scrapers/weld_county.py`](../src/land_survey_scraper/scrapers/weld_county.py))
should implement end-to-end.

The SOP was derived from a manual walkthrough by a licensed surveyor and documents three
distinct branches (Path A, B, C) based on what the Property Portal returns. Today our
scraper only implements Path A's happy path — Paths B and C are not yet automated.

---

## Inputs (variables the agent must resolve)

| Variable | Source | Notes |
|---|---|---|
| `{Subject_Property_Address}` | User input | Used for the primary Address search in the Property Portal. |
| `{APN_or_Account}` | Lookup result or direct input | Account number format `Rxxxxxxx` (e.g. `R1611986`). Parcel ID also accepted. Our CLI auto-detects `R\d{5,9}` and skips the address lookup. |
| `{Section}`, `{Township}`, `{Range}` | Lookup result (PLSS) | E.g. `15`, `5N`, `67W`. Some forms require zero-padded `01` or trailing direction letter — trial and error. |
| `{Owner_Name}` | Lookup result | The current record owner; may be an LLC or individual. Used as Grantor/Grantee fallback in Paths B and C. |
| `{Client_Name}` | User input | Surveyor's client; drives output folder naming. Not necessarily the property owner. |
| `{Output_Folder}` | Derived | Layout: `Client Name → Project Name → Instruments → [Document Type]`. |

---

## Phase 1 — Baseline Navigation (identical for all three paths)

1. Open the Weld County GIS landing page: `https://www.weld.gov/Government/Departments/Geographic-Information-Systems`.
2. Click the **Interactive Maps** tile → navigates to the Weld GIS Hub.
3. Click **View Property Portal** → opens `https://maps.weld.gov/propertyportal/` in a new tab.
   Also note the **Right of Way** theme on the same hub — pull any Road ROW documents from there too.
4. **Locate the parcel** using the first applicable lookup, in this priority order:
   1. **Address** (preferred) — toolbar → Address → type `{Subject_Property_Address}` → Enter.
   2. **STR fields** — Section / Township / Range search box.
   3. **S-T-R button** — explicit Section/Township/Range entry; zooms to section grid.
   4. **Owner** (last resort) — name search; ambiguous when LLCs hold multiple parcels.
5. Click inside the highlighted (orange) parcel boundary. The left rail switches to the
   **Identify Results** panel showing: Owner, Account, Parcel, Address, Subdivision, S/T/R,
   and hyperlinks to **Property Report**, **Data Search**, **Sales**.
   **Persist Owner / Account / Parcel / S-T-R to the run log** — they drive Paths B and C.
6. Click **Property Report** → new tab at
   `https://propertyportal.weld.gov/propertyrptlist.aspx?account={Account}`. The page renders
   a collapsed accordion (Owner(s), Document History, Building Info, Valuation, Tax, NOV/NOD,
   Photo, Sketch, Map, Print).
7. Expand **Document History**. This is the **Decision Frame** — the next phase routes on its
   exact content.

> **Implementation note.** Our scraper does not click through the GIS Hub → Interactive Maps
> tiles by default — it short-circuits straight to the same data via two HTTP endpoints
> (`apps.weld.gov/propertyportal/index.cfm` for the parcel resolve, and
> `propertyreport.weld.gov/?account=…` for the report). Every accordion section in the SOP's
> Step 1.6 page is server-rendered in a single response — the "Open/Close All Sections" click
> in the UI is purely visual. We parse all sections at once and write them to
> `overview.json`. The literal browser walk (SOP Steps 1.1 → 1.5) is still available via
> `--sop-strict` for demos.
>
> The **Map** accordion (the satellite view with the parcel boundary highlighted in red) is
> captured separately: Playwright renders the iframe URL
> `https://maps.weld.gov/mapanaccount/?Account={Account}` and screenshots the result to
> `tmp/{county}/{account}/map.png`. See [scrapers/weld_county.py](../src/land_survey_scraper/scrapers/weld_county.py)
> `_capture_map_image()`.

> **Note about URLs.** The SOP's `propertyportal.weld.gov/propertyrptlist.aspx?account=...`
> is the canonical Property Report. Our scraper currently uses `propertyreport.weld.gov/?account=...`
> which serves the same data via a newer skin. Both work today — keep both as fallbacks.

---

## Phase 2 — Decision Matrix

Evaluate the Document History table top-to-bottom and stop at the first matching condition.

| SOP letter | overview.json `path` | Condition | Visual trigger | Route to |
|---|---|---|---|---|
| **A** | `direct` | Table has rows AND contains both a vesting deed AND at least one `SURV` row | A `SURV` row with a clickable Reception number, e.g. `4970002` | **Path 3A** (Happy Path) |
| **B** | `alternate_partial` | Table has rows but is missing the survey, the vesting deed, or both | Deeds only (`WD`/`SWD`/`QCD`/`GEN`/`EXC`/`USR`) with no `SURV` row; or `SURV` rows but no vesting deed | **Path 3B** (Research 1) |
| **C** | `alternate_empty` | Table is empty and the literal string `No documents found.` is shown | Single text line beneath the section header | **Path 3C** (Research 2) |
| — | `unroutable` | Table empty but `No documents found.` text NOT present | (UI rendering error) | Retry, do not route |

**Tie-breakers**
- If both A and B technically match, default to A; fall through to B only if 3A.4 fails.
- If the table is still spinning after 15 s, reload the page once and re-evaluate.
- **Never** route to C unless the literal `No documents found.` is visible. A blank section
  due to a UI error is a retry case, not a Path C (this is treated as `UNROUTABLE` in code).

> **Implementation.** Wired in [scrapers/weld_county.py](../src/land_survey_scraper/scrapers/weld_county.py)
> as `_decision_matrix(records, html)`. Vesting deed types are `{WD, WDN, SWD, SWDN, QCD, QCN, QCDN, GEN}` — the SOP base set plus non-money variants seen in real data. The "Type"
> column in the Document History table uses `SURV` for all surveys including ALTA.
>
> The decision is persisted to `overview.json` as a top-level `decision_matrix` section
> with `path` (descriptive: `direct`, `alternate_partial`, `alternate_empty`, `unroutable`),
> `sop_letter` (`A`/`B`/`C`/`null`) for cross-reference with this SOP, human-readable
> `reasoning`, the matched `vesting_deeds` and `survey_rows`, and `most_recent_survey` /
> `most_recent_vesting_deed` (date-sorted). `meta.sop_path` is also set to the descriptive
> label so downstream code can branch on it.

---

## Phase 3A — Happy Path (Direct Extraction)

The ALTA was recorded against this parcel and is referenced in the GIS portal.

> **Implementation.** Wired in [scrapers/weld_county.py](../src/land_survey_scraper/scrapers/weld_county.py)
> as `_select_phase_3a_targets()` + `_download_documents()`. Runs only when
> `decision_matrix.path == "direct"`. Targets the `most_recent_survey` and
> `most_recent_vesting_deed` rows captured during the Decision Matrix. Each
> document is saved page-by-page as `tmp/{county}/{account}/{role}_{reception}_p{n}.pdf`
> (or `_{role}_{reception}.pdf` for single-page docs).
>
> **Disclaimer + reCAPTCHA bypass.** The disclaimer page at `recording.weld.gov`
> has a reCAPTCHA-gated "I Accept" button that headless Chromium can't pass.
> We inject the `disclaimerAccepted=true` cookie directly into the Playwright
> context — the document viewer only checks for that cookie's presence.
>
> **Login.** Anonymous viewing on `recording.weld.gov` returns a "must be a
> registered user" stub instead of the document images. We POST credentials
> directly to `/web/user/login` via `ctx.request.post()` (the in-page button
> is a jQuery Mobile fragment whose handler doesn't fire under Playwright).
> Set `WELD_RECORDER_USERNAME` / `WELD_RECORDER_PASSWORD` in `.env`. Register
> for free at `https://recording.weld.gov/web/user/register` if needed.
>
> Step 3A.5 (Schedule B-2 exception walk) is **not yet implemented** — it
> requires PDF text extraction from the saved ALTA to find `REC. NO.`
> references, then a Document Number lookup per reference.

1. **Click the `SURV` reception link** — the most recent `Type = SURV` row. A new tab opens
   the Tyler Tech Self Service Web at `https://recording.tylerhost.net/Welcome` with the
   document viewer loaded.
2. **Verify** the left metadata panel: Document Type contains `SURVEY` or `ALTA`; Recording
   Date is present; Grantor/Grantee includes `{Owner_Name}`; Section/Township/Range match.
   If verification fails, move to the next-most-recent `SURV` row, or fall through to Path 3B.
3. **Download the ALTA** via the printer/save icon in the viewer toolbar. Save as
   `{Output_Folder}/{Client_Name}_ALTA.pdf`. Wait until the file is on disk.
4. **Capture the vesting deed** — back to the Property Report tab, click the most recent row
   whose Type is in `{WD, SWD, GEN}`. Save as `{Client_Name}_VestingDeed_<Reception>.pdf`.
   Open it and read **Exhibit A — Legal Description**; persist S/T/R, plot bounds, and any
   cross-referenced reception numbers.
5. **Capture Schedule B-2 exceptions** — open the ALTA, locate Schedule B-2 and the
   survey-detail sheet. For each `REC. NO.` reference (e.g. `REC. NO. 1766550`), navigate to
   the Self Service Web → **Basic Search** → enter the Reception number in **Document Number**
   → Search → save as `{Client_Name}_Exception_<Reception>.pdf`.
6. Append a Path A completion record to the run log (parcel id, ALTA reception, supporting
   receptions, file paths).

---

## Phase 3B — Research Path 1 (rows present, but missing survey or deed)

> **Implementation.** Wired in [scrapers/weld_county.py](../src/land_survey_scraper/scrapers/weld_county.py)
> as `_select_phase_3b_targets()` + `_run_advanced_search()`. Runs only when
> `decision_matrix.path == "alternate_partial"`. Two passes:
>
> 1. **Step 3B.2** — most-recent vesting deed (if Document History has one).
>    Downloaded the same way Phase 3A grabs documents (`#printCustom` endpoint).
> 2. **Step 3B.4 + 3B.5** — Advanced Search at
>    `/web/search/DOCSEARCH524S12`. The form's direct HTTP POST endpoint
>    (`/web/searchPost/...`) returns only metadata, so we drive the page UI
>    via Playwright (`#field_PLSSLegalID_DOT_Section/Township/Range`, then
>    click `#searchButton`). Results are extracted from `li.ss-search-row`
>    elements: `data-documentid` (Tyler ID), header `<h1>` carrying
>    `<reception> • <type> • <date>`. Authentication is required — the
>    Advanced Search returns no rows for anonymous sessions.
> 3. **Step 3B.7** — if `parcel.subdivision` is non-empty, a second Advanced
>    Search runs with `Platted Legal → Subdivision`, and the result rows are
>    deduplicated against the S/T/R pass by reception number.
>
> The SOP's Document Types multiselect (`EASEMENT`, `RIGHT OF WAY`, etc.) is an
> autocomplete input that's awkward to drive headlessly, so we post-filter the
> result rows in Python against `_PHASE_3B_DOC_TYPES` instead. Match is loose:
> any row whose Type contains `EASEMENT`, `RIGHT OF WAY`, `R/W`, or `ROW` is
> kept.
>
> Step 3B.3 (Exhibit A cross-reference harvest) is **not implemented** — Tyler
> PDFs are scanned images with no text layer, so extracting `Excluding portions
> conveyed in Deed recorded …` references would require OCR (tesseract) or a
> vision LLM. The S/T/R Advanced Search in Step 3B.5 already finds all
> easements in the same section, so the practical recall loss is small.

The ALTA was either delivered out-of-band or never recorded. The agent must build the
easement / right-of-way packet via Advanced Search on S/T/R.

1. Re-scan the Document History; confirm the absence and persist visible Reception numbers
   and Types as candidates.
2. Open the most recent vesting deed (`SWD` / `WD` / `QCD` / `GEN`). Save as
   `{Client_Name}_VestingDeed_<Reception>.pdf`.
3. Open the PDF, read **Exhibit A** (usually pages 2–3). Extract:
   - Every Section/Township/Range listed (deeds can span multiple sections).
   - Every prior Reception number cited in "Excluding those portions conveyed in Deed
     recorded …" clauses.
4. Open the Self Service Web Advanced Search at `https://recording.tylerhost.net/Search/Advanced`.
   The form fields are: Document Number, Recording Date Start/End, Search Name, Grantor,
   Grantee, Platted Legal (Subdivision/Block/Tract/Lot/Unit), Unplatted Legal
   (Tract/Section/Township/Range), Legal Remarks, Book/Page, Document Types.
5. Under **Unplatted Legal**, enter Section / Township (numeric) / Range (numeric). Leave
   dates blank (defaults to `Jan 1, 1893 – today`). In **Document Types**, add the full
   easement filter list:
   ```
   EASEMENT, EASEMENT & RIGHT OF WAY, EASEMENT DEED, EASEMENT PLAT,
   EASEMENT RIGHT OF WAY & SURFACE USE AGR, GRANT & RELEASE OF EASEMENT,
   RIGHT OF WAY EASEMENT, R/W AGREEMENT, ROW, RIGHT OF WAY,
   RIGHT OF WAY AGREEMENT, AMENDED RIGHT OF WAY,
   EASEMENT RIGHT OF WAY AND SURFACE USE AGR, EASEMENT & SURFACE USE AGR,
   RIGHT OF WAY (RW)
   ```
6. **Capture every relevant row** whose Legal column matches S/T/R. Save as
   `{Client_Name}_ROW_<Reception>.pdf`. Cross-check against the cross-references from
   step 3 — flag any cited-but-missing.
7. **If the Identify Results panel listed a Subdivision name**, repeat step 5 with
   **Platted Legal — Subdivision** set to that name and Unplatted Legal cleared.
8. **External ALTA reconciliation** — check `{Output_Folder}` for any `*ALTA*.pdf` (client
   may have provided one). If present, rename to `{Client_Name}_ALTA.pdf` and reconcile its
   Schedule B-2 against the captured ROW docs. If not, log `NO RECORDED ALTA`.
9. Append a Path B completion record.

---

## Phase 3C — Research Path 2 (empty Document History)

Typical for large legacy agricultural parcels where transactions were recorded against the
parent owner across multiple sections rather than per-account. Drive everything from the
Self Service Web using `{Owner_Name}` and S/T/R.

1. Confirm the literal string `No documents found.` (anything else → retry, not Path C).
   Capture Owner, Account, Parcel from the page header.
2. Open `https://recording.tylerhost.net/Welcome` → click **Document Search** (or
   **Basic Search**).
3. **Owner-name search**: enter `{Owner_Name}` in **Search Name as Grantor or Grantee**.
   For each result whose Legal column matches the section (or any section listed on the
   parcel's exhibit), save as `{Client_Name}_<DocType>_<Reception>.pdf`. Pay particular
   attention to `AFFIDAVIT` and `QUIT CLAIM DEED` rows — these often carry the Exhibit A
   listing all contiguous parcels.
4. Open the most recent Quit Claim Deed; harvest every S/T/R from Exhibit A. This becomes
   the search universe for step 5.
5. **Subdivision-exemption Advanced Search** — set Unplatted Legal S/T/R, Document Types =
   `SUBDIVISION EXEMPTION, EXEMPTION, MINOR SUBDIVISION, AMENDED EXEMPTION`. Pull every
   result. Verify each title block reads `SUBDIVISION EXEMPTION NO. <id>` located in the
   matching S/T/R. Save as `{Client_Name}_SubdivisionExemption_<ExemptionNo>.pdf`.
6. **Easement / pipeline / ROW Advanced Search** — same S/T/R, easement filter list from
   Path 3B step 5. Save each as `{Client_Name}_ROW_<Reception>.pdf`. Watch for large
   utilities (Colorado Interstate Gas, Public Service Company, Poudre Valley REA) whose
   ROW agreements span multiple sections and may not surface via owner-name search.
7. **Last-ditch ALTA search** — same S/T/R, Document Types = `SURVEY, AMENDED SURVEY, ALTA SURVEY`.
   If a result matches, save as `{Client_Name}_ALTA.pdf`. Otherwise log
   `NO RECORDED ALTA — exemption packet only` (the exemption acts as the de facto survey).
8. Append a Path C completion record.

---

## Document Type Cheat Sheet

The Property Portal Document History uses short codes; the Self Service Web Advanced Search
uses full names. Map between them when building filter lists.

| Property Portal code | Document Type | Self Service Web filter value(s) |
|---|---|---|
| `SURV` | Survey (incl. ALTA Land Title Survey) | `SURVEY`, `ALTA SURVEY`, `AMENDED SURVEY` |
| `WD` | Warranty Deed | `WARRANTY DEED` |
| `SWD` | Special Warranty Deed | `SPECIAL WARRANTY DEED` |
| `QCD` | Quit Claim Deed | `QUIT CLAIM DEED` |
| `GEN` | General Warranty Deed | `GENERAL WARRANTY DEED` |
| `EXC` | Exception (Mineral / Surface) | `EXCEPTION`, `RESERVATION` |
| `USR` | Use by Special Review | `USE BY SPECIAL REVIEW` |
| `EASE` | Easement | `EASEMENT`, `EASEMENT DEED`, `EASEMENT PLAT` |
| `ROW` | Right of Way | `RIGHT OF WAY`, `RIGHT OF WAY EASEMENT`, `R/W AGREEMENT`, `AMENDED RIGHT OF WAY` |
| `SUBX` / `EXEMPT` | Subdivision Exemption | `SUBDIVISION EXEMPTION`, `EXEMPTION`, `MINOR SUBDIVISION`, `AMENDED EXEMPTION` |
| `AFF` | Affidavit | `AFFIDAVIT` |
| `NOV` / `NOD` | Notice of Valuation / Decision | `NOTICE OF VALUATION`, `NOTICE OF DECISION` |

The current `_SURVEY_TYPE_CODES` set in [`weld_county.py`](../src/land_survey_scraper/scrapers/weld_county.py)
covers most of these — keep that set as the source of truth for which docs to retain.

---

## URL Reference

| Purpose | URL |
|---|---|
| GIS landing | `https://www.weld.gov/Government/Departments/Geographic-Information-Systems` |
| GIS Hub — Interactive Maps | `https://gis.weld.lgcsrv.com/maps/interactive-maps` |
| Property Portal map | `https://maps.weld.gov/propertyportal/` |
| Property Portal — address search endpoint (used today) | `https://apps.weld.gov/propertyportal/index.cfm` |
| Property Report (SOP form) | `https://propertyportal.weld.gov/propertyrptlist.aspx?account={Account}` |
| Property Report (current scraper form) | `https://propertyreport.weld.gov/?account={Account}` |
| Self Service Web (Tyler Tech) | `https://recording.tylerhost.net/Welcome` |
| Self Service Web — Advanced Search | `https://recording.tylerhost.net/Search/Advanced` |
| Self Service Web — Document Detail | `https://recording.tylerhost.net/web/document/{ReceptionId}` |
| Local mirror (currently used in code) | `https://recording.weld.gov/web/web/integration/document/{ReceptionId}` |

> **Anonymous access works for ALTA / exemption / easement viewing.** The SOP is explicit:
> registration is only required to purchase certified copies. If the Self Service Web ever
> prompts for login, click **Cancel** and continue. This contradicts an earlier assumption
> that `recording.weld.gov` requires credentials — credentials may only be needed for the
> `recording.weld.gov` mirror, not for `recording.tylerhost.net`. **TODO**: verify which
> host actually requires auth and update the scraper accordingly.

---

## Worked Example — R1611986 (Stratus Delantero LLC)

This is the case study used to validate Path A. See `Case Study #1.pdf` in the project assets.

| Field | Value |
|---|---|
| Account | `R1611986` |
| Parcel | `095715000012` |
| Owner | `STRATUS DELANTERO LLC` |
| Section / Township / Range | `15 / 5N / 67W` |
| Acres (Calculated) | `303.570` |
| Legal | `GR 22S72 E2 15 S 67 (GOLD HILL #1 & #2 ANNEX) EXC UPRR RES (166)` |
| Sale Date | `2024-07-01` |
| Deed Code | `SWD` |
| Vesting Reception | `4970002` |
| Property Report URL | `https://propertyreport.weld.gov/?account=R1611986` |

Document History (filtered to survey-relevant types) returns 4 documents:
`WD (1999)`, `QCN (2008)`, `SURV (2020)`, `SWD (2024)`. Path A applies (both a `SURV`
row and a vesting deed are present). Expected outputs:

- `{Client}_ALTA.pdf` ← the 2020 SURV row
- `{Client}_VestingDeed_4970002.pdf` ← the 2024 SWD row
- `{Client}_Exception_<Reception>.pdf` for each Schedule B-2 reference

---

## Failure-Mode Quick Reference

| Failure | Recovery |
|---|---|
| Property Portal map fails to render | Hard refresh; confirm WebGL; fall back to the PDF Maps tile and supply parcel manually. |
| Identify Results shows multiple parcels (e.g. split by easement) | Process each parcel separately — independent SOP runs. |
| Self Service Web prompts for login | Click Cancel and continue. Anonymous is sufficient for viewing. |
| Schedule B-2 reception returns no result | Likely recorded in a different county or pre-1893; log and continue. |
| Document History expands but stays blank without `No documents found.` | UI error — reload once and retry. Do **not** treat as Path C. |
| Save dialog defaults to wrong folder | Type the absolute `{Output_Folder}` path into the filename field. |

---

## Gap Analysis vs. Current Code

Where the SOP exceeds today's [`weld_county.py`](../src/land_survey_scraper/scrapers/weld_county.py):

1. **Only Path A is implemented.** Paths 3B (S/T/R Advanced Search) and 3C (owner-name +
   exemption packet) are not yet automated. The scraper bails when Document History is
   empty or missing a `SURV` row.
2. **No Schedule B-2 follow-up.** Path A step 5 — opening the ALTA PDF, extracting
   `REC. NO.` references, and pulling each — is not implemented. Would need PDF text
   extraction (Claude API or `pypdf`).
3. **No vesting-deed Exhibit A parsing.** Cross-references inside the legal description are
   ignored; these are the input to step 3B.3's "excluded portions" harvest.
4. **No Advanced Search integration.** The scraper navigates the document viewer directly
   via reception number but does not drive the `recording.tylerhost.net/Search/Advanced`
   form for S/T/R / Document-Type queries.
5. **Output naming.** Today's files are `reception_<id>.<ext>`. The SOP specifies
   `{Client_Name}_<DocType>_<Reception>.pdf` under a client/project/instrument hierarchy.
6. **Anonymous vs. authenticated access.** Today's Phase 3 enforces login. The SOP says
   anonymous works for the Tyler Tech host. Worth A/B-testing before requiring credentials.
7. **Right of Way theme.** The SOP notes that road ROW documents may live under the GIS
   Hub's **Right of Way** theme — separate from Document History. Not yet scraped.

These gaps form the natural roadmap for completing Weld parity with the SOP.
