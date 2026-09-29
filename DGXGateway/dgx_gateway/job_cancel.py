"""Durable cancellation intent separate from the worker-owned job snapshot."""
import json
import os
import time


class JobCanceled(Exception):
    pass


class CancelableJobs:
    def cancel_requested(self, job_id):
        return (self.root / (job_id + ".cancel")).is_file()

    def check_cancel(self, row):
        if self.cancel_requested(row["job_id"]):
            raise JobCanceled("Cancellation requested")

    def cancel(self, job_id):
        with self.app._submit_lock:
            row = self.get(job_id)  # validates identity before constructing a path
            if row["state"] in {"SUCCEEDED", "FAILED", "INTERRUPTED", "CANCELED"}:
                return row
            path = self.root / (job_id + ".cancel")
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "w") as stream:
                    json.dump({"requested_at": time.time()}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
            return self.get(job_id)

    def cancel_view(self, row):
        row["cancel_requested"] = self.cancel_requested(row["job_id"])
        if row["cancel_requested"] and row["state"] not in {"SUCCEEDED", "FAILED", "INTERRUPTED", "CANCELED"}:
            row["cancel_status"] = self.cancel_boundary
        return row
