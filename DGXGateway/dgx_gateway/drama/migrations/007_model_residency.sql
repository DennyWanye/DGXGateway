BEGIN IMMEDIATE;
ALTER TABLE dr_video_jobs ADD COLUMN resource_settled INTEGER NOT NULL DEFAULT 0;
ALTER TABLE dr_video_jobs ADD COLUMN resource_status TEXT NOT NULL DEFAULT 'unsettled';
UPDATE dr_video_jobs SET resource_settled=1,resource_status='released' WHERE memory_released=1 AND cleanup_required=0 AND state IN ('SUCCEEDED','FAILED','CANCELED','INTERRUPTED');
PRAGMA user_version=7;
COMMIT;
