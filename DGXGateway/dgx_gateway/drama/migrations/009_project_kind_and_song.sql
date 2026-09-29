BEGIN IMMEDIATE;
ALTER TABLE dr_projects ADD COLUMN kind TEXT NOT NULL DEFAULT 'drama' CHECK(kind IN ('drama','mv'));
ALTER TABLE dr_episodes ADD COLUMN song_asset_id TEXT REFERENCES dr_assets(id);
PRAGMA user_version=9;
COMMIT;
