BEGIN IMMEDIATE;
CREATE TABLE dr_reference_jobs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES dr_projects(id),
    target_type TEXT NOT NULL CHECK(target_type IN ('entity','shot')),
    target_id TEXT NOT NULL,
    target_revision_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    parameters TEXT NOT NULL,
    input_snapshot TEXT NOT NULL,
    dispatch_key TEXT NOT NULL UNIQUE,
    gateway_job_id TEXT,
    state TEXT NOT NULL,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(gateway_job_id)
);
CREATE TABLE dr_reference_candidates (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES dr_projects(id),
    target_type TEXT NOT NULL CHECK(target_type IN ('entity','shot')),
    target_id TEXT NOT NULL,
    target_revision_id TEXT NOT NULL,
    asset_id TEXT NOT NULL REFERENCES dr_assets(id),
    job_id TEXT NOT NULL REFERENCES dr_reference_jobs(id),
    source_key TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(job_id,source_key)
);
CREATE INDEX dr_reference_jobs_project ON dr_reference_jobs(project_id,created_at);
CREATE INDEX dr_reference_candidates_target ON dr_reference_candidates(target_type,target_id);
PRAGMA user_version=3;
COMMIT;
