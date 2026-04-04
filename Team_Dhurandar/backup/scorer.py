"""
=============================================================
  AI Priority Engine — scorer.py
  Person 2 | Hackathon Backup Optimizer
=============================================================

  PURPOSE:
    Reads file metadata (from teammates' output) and assigns
    each file a priority_score (0–100) and a priority_label
    (high / medium / low).

  SCORING BREAKDOWN (total = 100 points):
    - Recency score      : up to 40 pts  (how recently modified)
    - File type score    : up to 30 pts  (docs/images > temp/logs)
    - File size score    : up to 15 pts  (smaller critical files first)
    - Keyword score      : up to 15 pts  (important words in filename)

  DOES NOT:
    - Handle file scanning, hashing, deduplication (teammates' job)
    - Upload files (your Step 3)
    - Connect to any cloud or DB
=============================================================
"""

import time


# ------------------------------------------------------------------
# CONFIGURATION — Tune these constants to change scoring behaviour
# ------------------------------------------------------------------

# File type tiers: higher value = higher priority
FILE_TYPE_SCORES = {
    # Tier 1 — Critical documents (30 pts)
    ".pdf": 30, ".docx": 30, ".doc": 30, ".xlsx": 30,
    ".xls": 30, ".pptx": 30, ".txt": 25,

    # Tier 2 — Media files (20 pts)
    ".jpg": 20, ".jpeg": 20, ".png": 20, ".gif": 20,
    ".mp4": 20, ".mov": 20, ".mp3": 20,

    # Tier 3 — Code / config files (15 pts)
    ".py": 15, ".js": 15, ".html": 15, ".css": 15,
    ".json": 15, ".xml": 15, ".yaml": 15, ".yml": 15,

    # Tier 4 — Archives (10 pts)
    ".zip": 10, ".tar": 10, ".gz": 10, ".rar": 10,

    # Tier 5 — Logs / temp (5 pts — low priority)
    ".log": 5, ".tmp": 5, ".bak": 5, ".cache": 5,
}

# Keywords that signal importance (case-insensitive)
IMPORTANT_KEYWORDS = [
    "final", "report", "project", "important", "critical",
    "urgent", "master", "backup", "submit", "invoice",
    "contract", "resume", "cv", "thesis", "presentation",
]

# Priority label thresholds
LABEL_HIGH_THRESHOLD   = 65   # score >= 65 → "high"
LABEL_MEDIUM_THRESHOLD = 35   # score >= 35 → "medium"
                               # score <  35 → "low"

# Recency windows (in seconds)
ONE_HOUR   = 3600
ONE_DAY    = 86400
ONE_WEEK   = 604800
ONE_MONTH  = 2592000


# ------------------------------------------------------------------
# INTERNAL HELPER FUNCTIONS
# Each function handles ONE scoring dimension.
# ------------------------------------------------------------------

def _score_recency(last_modified_time: float) -> int:
    """
    Awards points based on how recently the file was modified.

    Args:
        last_modified_time: Unix timestamp (float/int)

    Returns:
        int: 0–40 points
    """
    now = time.time()
    age_seconds = now - last_modified_time  # how old is the file?

    if age_seconds <= 0:
        # Modified right now or in the future — top score
        return 40
    elif age_seconds <= ONE_HOUR:
        return 40   # Modified within the last hour
    elif age_seconds <= ONE_DAY:
        return 30   # Modified within the last day
    elif age_seconds <= ONE_WEEK:
        return 20   # Modified within the last week
    elif age_seconds <= ONE_MONTH:
        return 10   # Modified within the last month
    else:
        return 2    # Old file — very low recency score


def _score_file_type(file_type: str) -> int:
    """
    Awards points based on the file's extension (type).

    Args:
        file_type: File extension string like ".pdf", ".log"

    Returns:
        int: 0–30 points
    """
    # Normalize: lowercase and ensure it starts with "."
    ext = file_type.lower().strip()
    if ext and not ext.startswith("."):
        ext = "." + ext

    # Look up in our type → score table, default to 8 if unknown
    return FILE_TYPE_SCORES.get(ext, 8)


def _score_file_size(file_size: int) -> int:
    """
    Awards points favouring smaller critical files.
    Rationale: a 10KB config file is often more "critical" than
    a 4GB video archive in a backup context.

    Args:
        file_size: File size in bytes

    Returns:
        int: 0–15 points
    """
    one_kb = 1024
    one_mb = 1024 * 1024
    one_gb = 1024 * 1024 * 1024

    if file_size <= 0:
        return 5   # Unknown or empty size — neutral score

    if file_size <= 100 * one_kb:       # ≤ 100 KB
        return 15
    elif file_size <= 10 * one_mb:      # ≤ 10 MB
        return 12
    elif file_size <= 100 * one_mb:     # ≤ 100 MB
        return 8
    elif file_size <= one_gb:           # ≤ 1 GB
        return 4
    else:                               # > 1 GB
        return 1


def _score_keywords(file_name: str) -> int:
    """
    Awards points if the filename contains important keywords.

    Args:
        file_name: The filename string, e.g. "final_report_v2.pdf"

    Returns:
        int: 0–15 points
    """
    name_lower = file_name.lower()
    matched_count = 0

    for keyword in IMPORTANT_KEYWORDS:
        if keyword in name_lower:
            matched_count += 1

    # Award points: 15 for 2+ matches, 10 for 1, 0 for none
    if matched_count >= 2:
        return 15
    elif matched_count == 1:
        return 10
    else:
        return 0


def _get_priority_label(score: int) -> str:
    """
    Converts a numeric score into a human-readable label.

    Args:
        score: Numeric priority score (0–100)

    Returns:
        str: "high", "medium", or "low"
    """
    if score >= LABEL_HIGH_THRESHOLD:
        return "high"
    elif score >= LABEL_MEDIUM_THRESHOLD:
        return "medium"
    else:
        return "low"


# ------------------------------------------------------------------
# MAIN PUBLIC FUNCTION
# This is what the rest of the system will call.
# ------------------------------------------------------------------

def calculate_priority(file: dict) -> dict:
    """
    Takes a single file metadata dict and returns it enriched
    with priority_score and priority_label.

    Args:
        file (dict): Must contain:
            - file_path        (str)
            - file_name        (str)
            - file_size        (int, bytes)
            - last_modified_time (float, Unix timestamp)
            - file_type        (str, e.g. ".pdf")
            - is_duplicate     (bool)
            - is_changed       (bool)

    Returns:
        dict: Original file dict + added keys:
            - priority_score   (int, 0–100)
            - priority_label   (str, "high"/"medium"/"low")
            - score_breakdown  (dict, for debugging/transparency)

    Example:
        >>> file = {
        ...     "file_path": "/docs/final_report.pdf",
        ...     "file_name": "final_report.pdf",
        ...     "file_size": 204800,
        ...     "last_modified_time": time.time() - 3600,
        ...     "file_type": ".pdf",
        ...     "is_duplicate": False,
        ...     "is_changed": True
        ... }
        >>> result = calculate_priority(file)
        >>> print(result["priority_label"])
        'high'
    """

    # --- Extract fields (with safe defaults) ---
    file_name          = file.get("file_name", "unknown")
    file_size          = file.get("file_size", 0)
    last_modified_time = file.get("last_modified_time", 0)
    file_type          = file.get("file_type", "")

    # --- Calculate each sub-score ---
    recency_score  = _score_recency(last_modified_time)
    type_score     = _score_file_type(file_type)
    size_score     = _score_file_size(file_size)
    keyword_score  = _score_keywords(file_name)

    # --- Add all sub-scores for total ---
    total_score = recency_score + type_score + size_score + keyword_score

    # --- Cap between 0 and 100 (safety measure) ---
    total_score = max(0, min(100, total_score))

    # --- Determine label ---
    priority_label = _get_priority_label(total_score)

    # --- Build the breakdown (useful for debugging and transparency) ---
    score_breakdown = {
        "recency_score":  recency_score,   # max 40
        "type_score":     type_score,      # max 30
        "size_score":     size_score,      # max 15
        "keyword_score":  keyword_score,   # max 15
        "total":          total_score,     # max 100
    }

    # --- Return enriched file dict (non-destructive: original fields preserved) ---
    return {
        **file,                              # all original fields from teammates
        "priority_score":   total_score,
        "priority_label":   priority_label,
        "score_breakdown":  score_breakdown,
    }


def prioritize_files(files: list) -> list:
    """
    Processes a list of file dicts and returns them all with
    priority scores added. Does NOT sort or filter (that's
    the Scheduler's job in Step 2).

    Args:
        files (list): List of file metadata dicts from teammates

    Returns:
        list: Same files, each enriched with priority_score,
              priority_label, and score_breakdown
    """
    if not files:
        return []

    scored_files = []
    for file in files:
        scored_file = calculate_priority(file)
        scored_files.append(scored_file)

    return scored_files