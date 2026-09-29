BEGIN IMMEDIATE;
CREATE TABLE dr_production_state (
 project_id TEXT PRIMARY KEY REFERENCES dr_projects(id), paused INTEGER NOT NULL DEFAULT 0,
 revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE dr_video_jobs (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES dr_projects(id),
 episode_id TEXT NOT NULL REFERENCES dr_episodes(id), dispatch_key TEXT NOT NULL UNIQUE,
 gateway_job_id TEXT UNIQUE, state TEXT NOT NULL, error TEXT,
 memory_released INTEGER NOT NULL DEFAULT 0, cleanup_required INTEGER NOT NULL DEFAULT 0,
 cancel_requested INTEGER NOT NULL DEFAULT 0, reserved_shots INTEGER NOT NULL,
 created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE dr_video_items (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES dr_video_jobs(id), ordinal INTEGER NOT NULL,
 shot_id TEXT NOT NULL REFERENCES dr_shots(id), shot_revision_id TEXT NOT NULL REFERENCES dr_shot_revisions(id),
 input_snapshot TEXT NOT NULL, state TEXT NOT NULL, submission_phase TEXT NOT NULL DEFAULT 'NOT_SUBMITTED',
 error TEXT, take_id TEXT REFERENCES dr_takes(id), output_hash TEXT,
 UNIQUE(job_id,ordinal)
);
CREATE TABLE dr_take_reviews (
 take_id TEXT PRIMARY KEY REFERENCES dr_takes(id), decision TEXT NOT NULL,
 notes TEXT NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE dr_video_continuations (source_job_id TEXT PRIMARY KEY REFERENCES dr_video_jobs(id), continuation_job_id TEXT NOT NULL UNIQUE REFERENCES dr_video_jobs(id));
CREATE INDEX dr_video_jobs_project ON dr_video_jobs(project_id,created_at);
PRAGMA user_version=4;
COMMIT;
