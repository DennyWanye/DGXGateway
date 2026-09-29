BEGIN IMMEDIATE;
CREATE TABLE dr_reference_candidate_reviews (
    candidate_id TEXT PRIMARY KEY REFERENCES dr_reference_candidates(id),
    project_id TEXT NOT NULL REFERENCES dr_projects(id),
    decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
    notes TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX dr_reference_candidate_reviews_project ON dr_reference_candidate_reviews(project_id,updated_at);
PRAGMA user_version=8;
COMMIT;
