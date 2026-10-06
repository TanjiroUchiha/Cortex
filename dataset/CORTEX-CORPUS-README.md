# Cortex Expanded Synthetic Corpus

This standalone package contains fictional institutional knowledge for retrieval, multi-document, versioning and governance experiments. It is not university policy and must not be used to make real operational decisions. Names, amounts, schedules, IDs and contacts are synthetic; `.example` email domains are reserved placeholders.

The corpus is organized into 11 routing folders. Admissions files are placed under `hr`; Student Services files are placed under `general`; Hostel files are placed under `facilities`; and Labs files are placed under `research`. Each moved file has an `original_category` field, and its `department` metadata retains the source owning department. Markdown files use YAML frontmatter with document identity, department/category, entity, document type, sensitivity, allowed roles, version, effective date and status. Selected records also have `allowed_users` and entity-specific scopes. These labels describe test expectations; they do not enforce authorization by themselves.

This package is intentionally standalone: the current Cortex router accepts only `it`, `hr`, `fees`, `facilities` and `general`, so it cannot ingest these 15 top-level categories without a corresponding application update. The original corpus sources were copied/converted into this package and seed-corpus details were merged into their corresponding documents; the original working files were not changed.

See `manifest.json` for per-document metadata and `evaluation/` for retrieval, governance, multi-document and negative-query datasets. Evaluation paths are relative to this package root. Active versions should be preferred over records whose metadata marks them `superseded`.
