BEGIN IMMEDIATE;
CREATE TABLE dr_subtitle_revisions (
 id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES dr_episodes(id),
 timeline_id TEXT NOT NULL REFERENCES dr_timeline_revisions(id),
 revision INTEGER NOT NULL, snapshot TEXT NOT NULL, created_at REAL NOT NULL,
 UNIQUE(episode_id,revision)
);
CREATE TABLE dr_subtitle_approvals (
 subtitle_id TEXT PRIMARY KEY REFERENCES dr_subtitle_revisions(id),
 approved_at REAL NOT NULL
);
CREATE TABLE dr_export_recipes (
 export_id TEXT PRIMARY KEY REFERENCES dr_exports(id), recipe TEXT NOT NULL,
 outputs TEXT NOT NULL DEFAULT '[]', review TEXT
);
CREATE TABLE dr_transcriptions (
 id TEXT PRIMARY KEY, timeline_id TEXT NOT NULL REFERENCES dr_timeline_revisions(id),
 state TEXT NOT NULL, subtitle_id TEXT REFERENCES dr_subtitle_revisions(id),
 audio_asset_id TEXT REFERENCES dr_assets(id), error TEXT,
 created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE dr_transcription_details (
 job_id TEXT PRIMARY KEY REFERENCES dr_transcriptions(id),
 base_revision INTEGER NOT NULL, result TEXT
);
PRAGMA user_version=5;
COMMIT;
