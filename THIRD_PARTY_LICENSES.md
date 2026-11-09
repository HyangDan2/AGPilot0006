# Third-Party Dependencies and License Audit

This document audits all direct and transitive third-party dependencies used in **HD2Lab Document Intelligence Service (AGPilot0006)** in accordance with Phase 2 Work Order C-3.

---

## 1. Direct Dependency License Summary

| Package | Version | License | Primary Role | Notes & Compliance |
| :--- | :--- | :--- | :--- | :--- |
| **fastapi** | `>=0.115.0` | MIT | Web API Framework | Fully permissive; commercial use permitted. |
| **uvicorn** | `>=0.32.0` | BSD-3-Clause | ASGI Server | Permissive. |
| **pydantic** | `>=2.9.0` | MIT | Data Validation & Schemas | Permissive. |
| **python-multipart**| `>=0.0.18`| Apache-2.0 | Streaming File Uploads | Permissive. |
| **python-docx** | `>=1.1.2` | MIT | DOCX Text & Structure Extraction | Fully permissive. |
| **python-pptx** | `>=1.0.2` | MIT | PPTX Slide & Shape Extraction | Fully permissive. |
| **Pillow** | `>=11.0.0` | HPND | Image Inspection & Transformation | Permissive, BSD-like. |
| **PyMuPDF** | `>=1.25.0` | **GNU AGPL v3.0 / Commercial** | PDF Parsing & Page Rasterization | **Audit Required (See Section 2)** |
| **pymupdf4llm** | `>=0.0.17` | GNU AGPL v3.0 | PDF Extraction Addon | AGPL v3.0 |
| **pytest** | `>=8.3.0` | MIT | Test Runner (Dev) | Dev dependency only. |
| **httpx** | `>=0.28.0` | BSD-3-Clause | Test HTTP Client (Dev) | Dev dependency only. |

---

## 2. PyMuPDF (AGPL v3.0) Evaluation & Operational Guidelines

### A. Current Status
`PyMuPDF` (and `pymupdf4llm`) is licensed under the **GNU Affero General Public License v3.0 (AGPL v3.0)** for open-source distributions, or under an Artifex Commercial License for proprietary closed deployments.

### B. Impact on AGPilot0006 Architecture
1. **Network API Boundary**:
   - `AGPilot0006` is architected as an **independent, standalone microservice** communicating exclusively via REST API over HTTP.
   - It is deliberately decoupled from `AGPilot0005`, `AGCore`, and client applications.
2. **Open-Source Compliance**:
   - For open-source deployment (e.g., public GitHub repository under AGPL/GPL), sharing the source code of `AGPilot0006` satisfies AGPL obligations.
3. **Proprietary / Closed Enterprise Deployment Options**:
   - **Option 1 (Recommended for Enterprise)**: Acquire an Artifex PyMuPDF Commercial License.
   - **Option 2 (Permissive Alternative)**: Swap the PDF parser backend to `pypdf` (BSD-3-Clause) + `pdf2image` (MIT) with `poppler` CLI, or pure Python PDF engines (`pdfplumber` / `pypdf`).
   - The modular `BaseParser` and `PDFRenderer` interface in `app/parsers/` allows switching renderer backends without changing any API contract or business logic.

---

## 3. External System Tools

| Tool | License | Role | Status |
| :--- | :--- | :--- | :--- |
| **LibreOffice** | MPL 2.0 / LGPL v3+ | PPTX Headless PDF Conversion | Executed via CLI subprocess isolation; no library linkage. Permissive for service use. |
| **SQLite 3** | Public Domain | Metadata DB & FTS5 Search | Built into Python runtime; fully permissive. |

---

## 4. SBOM (Software Bill of Materials) Inventory
All dependencies are pinned in `requirements.txt` with minimum tested versions to ensure deterministic, reproducible builds.
