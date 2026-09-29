"""Bounded diagnostic admission; lifecycle remains owned by VideoJobs."""


def validate_diagnostic(value):
    if value is None:
        return None
    fields = {"trace_step", "max_steps", "max_seconds"}
    if not isinstance(value, dict) or not fields <= set(value) or set(value) - fields - {"attention_probe"}:
        raise ValueError("invalid diagnostic options")
    if any(type(value[key]) is not int for key in fields):
        raise ValueError("diagnostic options must be integers")
    if not 1 <= value["max_steps"] <= 6 or not 60 <= value["max_seconds"] <= 3600:
        raise ValueError("diagnostic budget out of range")
    if not -1 <= value["trace_step"] < value["max_steps"]:
        raise ValueError("trace_step must be -1 or inside the diagnostic budget")
    if "attention_probe" in value and type(value["attention_probe"]) is not bool:
        raise ValueError("attention_probe must be boolean")
    if value.get("attention_probe") and (value["trace_step"] != -1 or value["max_steps"] != 1):
        raise ValueError("attention probe requires trace off and exactly one step")
    return dict(value)


def should_stop(row, now):
    options = row["diagnostic"]
    # Absolute persisted deadline also survives a Gateway reconcile/restart.
    if now - row["diagnostic_started_at"] >= options["max_seconds"]:
        return "time_budget"
    progress = row.get("diagnostic_progress", {})
    if all(progress.get(str(rank), {}).get("completed_steps", 0) >= options["max_steps"] for rank in range(4)):
        return "step_budget"
    return None
