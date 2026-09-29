BEGIN IMMEDIATE;
ALTER TABLE dr_shot_revisions ADD COLUMN id TEXT;
UPDATE dr_shot_revisions SET id=lower(hex(randomblob(16)));
CREATE UNIQUE INDEX dr_content_revision_id ON dr_shot_revisions(id);
ALTER TABLE dr_shots ADD COLUMN current_content_revision_id TEXT REFERENCES dr_shot_revisions(id);
UPDATE dr_shots SET current_content_revision_id=(SELECT id FROM dr_shot_revisions r WHERE r.shot_id=dr_shots.id ORDER BY r.revision DESC LIMIT 1);
ALTER TABLE dr_takes ADD COLUMN source_revision_id TEXT REFERENCES dr_shot_revisions(id);
UPDATE dr_takes SET source_revision_id=(SELECT current_content_revision_id FROM dr_shots WHERE id=dr_takes.shot_id);
CREATE TABLE dr_entities (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES dr_projects(id), kind TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1, current_revision_id TEXT REFERENCES dr_entity_revisions(id),
 approved_revision_id TEXT REFERENCES dr_entity_revisions(id), created_at REAL NOT NULL
);
CREATE TABLE dr_entity_revisions (
 id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES dr_entities(id), revision INTEGER NOT NULL,
 snapshot TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(entity_id,revision)
);
CREATE TABLE dr_entity_reference_bindings (
 revision_id TEXT NOT NULL REFERENCES dr_entity_revisions(id), asset_id TEXT NOT NULL REFERENCES dr_assets(id),
 role TEXT NOT NULL, PRIMARY KEY(revision_id,role)
);
CREATE TABLE dr_dependencies (
 shot_revision_id TEXT NOT NULL REFERENCES dr_shot_revisions(id),
 entity_revision_id TEXT NOT NULL REFERENCES dr_entity_revisions(id),
 role TEXT NOT NULL, PRIMARY KEY(shot_revision_id,entity_revision_id,role)
);
CREATE TABLE dr_invalidations (
 id TEXT PRIMARY KEY, shot_revision_id TEXT NOT NULL REFERENCES dr_shot_revisions(id),
 entity_id TEXT NOT NULL REFERENCES dr_entities(id), old_revision_id TEXT NOT NULL REFERENCES dr_entity_revisions(id),
 new_revision_id TEXT NOT NULL REFERENCES dr_entity_revisions(id), reason TEXT NOT NULL,
 resolved_at REAL, resolution TEXT, created_at REAL NOT NULL,
 approval_id TEXT NOT NULL,
 UNIQUE(shot_revision_id,entity_id,approval_id)
);
CREATE TABLE dr_production_approvals (
 id TEXT PRIMARY KEY, target_type TEXT NOT NULL, target_id TEXT NOT NULL,
 revision_id TEXT NOT NULL, asset_id TEXT REFERENCES dr_assets(id), created_at REAL NOT NULL
);
CREATE INDEX dr_entities_project ON dr_entities(project_id);
CREATE INDEX dr_deps_entity_revision ON dr_dependencies(entity_revision_id);
CREATE INDEX dr_invalidations_target ON dr_invalidations(shot_revision_id,resolved_at);
PRAGMA user_version=2;
COMMIT;
