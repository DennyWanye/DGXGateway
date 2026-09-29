BEGIN IMMEDIATE;
CREATE TABLE dr_story_revisions (
 id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES dr_episodes(id),
 kind TEXT NOT NULL CHECK(kind IN ('source','entities','beats','script','storyboard')),
 revision INTEGER NOT NULL, content TEXT NOT NULL, content_hash TEXT NOT NULL,
 dependencies TEXT NOT NULL, created_at REAL NOT NULL,
 UNIQUE(episode_id,kind,revision)
);
CREATE TABLE dr_story_heads (
 episode_id TEXT NOT NULL REFERENCES dr_episodes(id), kind TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 0,
 current_revision_id TEXT REFERENCES dr_story_revisions(id),
 approved_revision_id TEXT REFERENCES dr_story_revisions(id),
 PRIMARY KEY(episode_id,kind)
);
CREATE TABLE dr_story_approvals (
 id TEXT PRIMARY KEY, revision_id TEXT NOT NULL REFERENCES dr_story_revisions(id), created_at REAL NOT NULL,
 UNIQUE(revision_id)
);
CREATE TABLE dr_story_drafts (
 id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES dr_episodes(id), kind TEXT NOT NULL,
 revision INTEGER NOT NULL, raw_text TEXT NOT NULL, raw_hash TEXT NOT NULL,
 validation TEXT NOT NULL, attempt_id TEXT, created_at REAL NOT NULL,
 UNIQUE(episode_id,kind,revision), UNIQUE(attempt_id)
);
CREATE TABLE dr_entity_proposal_mappings (
 episode_id TEXT NOT NULL REFERENCES dr_episodes(id), candidate_id TEXT NOT NULL,
 proposal_revision_id TEXT NOT NULL REFERENCES dr_story_revisions(id),
 entity_id TEXT NOT NULL REFERENCES dr_entities(id), entity_revision_id TEXT NOT NULL REFERENCES dr_entity_revisions(id),
 sources TEXT NOT NULL, created_at REAL NOT NULL,
 PRIMARY KEY(episode_id,candidate_id)
);
CREATE TABLE dr_storyboard_mappings (
 episode_id TEXT NOT NULL REFERENCES dr_episodes(id), candidate_id TEXT NOT NULL,
 proposal_revision_id TEXT NOT NULL REFERENCES dr_story_revisions(id),
 shot_id TEXT NOT NULL REFERENCES dr_shots(id), shot_revision_id TEXT NOT NULL REFERENCES dr_shot_revisions(id),
 created_at REAL NOT NULL, PRIMARY KEY(episode_id,candidate_id)
);
CREATE TABLE dr_script_dependencies (
 shot_revision_id TEXT PRIMARY KEY REFERENCES dr_shot_revisions(id),
 script_revision_id TEXT NOT NULL REFERENCES dr_story_revisions(id), scene_id TEXT NOT NULL,
 dialogue TEXT NOT NULL
);
CREATE TABLE dr_script_invalidations (
 id TEXT PRIMARY KEY, shot_revision_id TEXT NOT NULL REFERENCES dr_shot_revisions(id),
 old_revision_id TEXT NOT NULL REFERENCES dr_story_revisions(id), new_revision_id TEXT NOT NULL REFERENCES dr_story_revisions(id),
 reason TEXT NOT NULL, resolved_at REAL, resolution TEXT, created_at REAL NOT NULL,
 UNIQUE(shot_revision_id,new_revision_id)
);
CREATE TABLE dr_text_jobs (
 id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES dr_episodes(id),
 kind TEXT NOT NULL, request_key TEXT NOT NULL, input_hash TEXT NOT NULL, payload TEXT NOT NULL,
 state TEXT NOT NULL, output_status TEXT NOT NULL DEFAULT 'none', resource_status TEXT NOT NULL DEFAULT 'not_acquired',
 gateway_id TEXT, current_attempt_id TEXT, epoch INTEGER NOT NULL DEFAULT 0, error TEXT,
 repair_of TEXT REFERENCES dr_text_jobs(id), created_at REAL NOT NULL, updated_at REAL NOT NULL,
 UNIQUE(episode_id,request_key), UNIQUE(repair_of)
);
CREATE TABLE dr_text_attempts (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES dr_text_jobs(id), epoch INTEGER NOT NULL,
 payload_hash TEXT NOT NULL, dispatch_state TEXT NOT NULL, raw_text TEXT, raw_hash TEXT,
 validation TEXT, receipt TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 UNIQUE(job_id,epoch)
);
CREATE TABLE dr_text_receipt_evidence (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES dr_text_jobs(id), attempt_id TEXT NOT NULL,
 receipt TEXT NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX dr_story_episode ON dr_story_revisions(episode_id,kind);
CREATE INDEX dr_script_dependency ON dr_script_dependencies(script_revision_id);
CREATE INDEX dr_text_episode ON dr_text_jobs(episode_id,created_at);
PRAGMA user_version=6;
COMMIT;
