BEGIN IMMEDIATE;
CREATE TABLE dr_projects (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, width INTEGER NOT NULL,
    height INTEGER NOT NULL, fps INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE TABLE dr_episodes (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES dr_projects(id),
    title TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
    timeline_revision INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL
);
CREATE TABLE dr_assets (
    id TEXT PRIMARY KEY, filename TEXT NOT NULL, metadata TEXT NOT NULL,
    size_bytes INTEGER NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE dr_asset_bindings (
    project_id TEXT NOT NULL REFERENCES dr_projects(id),
    asset_id TEXT NOT NULL REFERENCES dr_assets(id),
    PRIMARY KEY(project_id,asset_id)
);
CREATE TABLE dr_shots (
    id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES dr_episodes(id),
    title TEXT NOT NULL, ordinal INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
    selected_take_id TEXT REFERENCES dr_takes(id), created_at REAL NOT NULL,
    UNIQUE(episode_id,ordinal)
);
CREATE TABLE dr_shot_revisions (
    shot_id TEXT NOT NULL REFERENCES dr_shots(id), revision INTEGER NOT NULL,
    snapshot TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(shot_id,revision)
);
CREATE TABLE dr_takes (
    id TEXT PRIMARY KEY, shot_id TEXT NOT NULL REFERENCES dr_shots(id),
    asset_id TEXT NOT NULL REFERENCES dr_assets(id), source TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE dr_timeline_revisions (
    id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES dr_episodes(id),
    revision INTEGER NOT NULL, snapshot TEXT NOT NULL, created_at REAL NOT NULL,
    UNIQUE(episode_id,revision)
);
CREATE TABLE dr_exports (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES dr_projects(id),
    episode_id TEXT NOT NULL REFERENCES dr_episodes(id),
    timeline_id TEXT NOT NULL REFERENCES dr_timeline_revisions(id),
    state TEXT NOT NULL, asset_id TEXT REFERENCES dr_assets(id), error TEXT,
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE dr_requests (
    scope TEXT NOT NULL, request_key TEXT NOT NULL, input_hash TEXT NOT NULL,
    response TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(scope,request_key)
);
CREATE TABLE dr_events (
    id TEXT PRIMARY KEY, scope TEXT NOT NULL, kind TEXT NOT NULL,
    payload TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX dr_episode_project ON dr_episodes(project_id);
CREATE INDEX dr_shot_episode ON dr_shots(episode_id);
CREATE INDEX dr_take_shot ON dr_takes(shot_id);
CREATE INDEX dr_export_project ON dr_exports(project_id);
PRAGMA user_version=1;
COMMIT;
