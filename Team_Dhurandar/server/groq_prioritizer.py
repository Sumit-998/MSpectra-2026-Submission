"""
groq_prioritizer.py
--------------------
Uses Groq LLM to intelligently prioritize backed-up files.
Called by the /ai/prioritize endpoint in metadata_server.py.
"""

import json
import os
import time
from groq import Groq


def prioritize_files_with_groq(files: list[dict], api_key: str) -> list[dict]:
    """
    Sends file metadata to Groq and gets back an AI-ranked priority list.

    Args:
        files:   List of file metadata dicts from the backup server
        api_key: Groq API key

    Returns:
        List of files sorted by AI-assigned priority with explanation
    """
    client = Groq(api_key=api_key)

    # Build a compact summary of files for the prompt
    file_summaries = []
    for i, f in enumerate(files):
        age_days = (time.time() - f.get("backed_up_at", time.time())) / 86400
        file_summaries.append(
            f"{i+1}. {f['path']} | size: {f.get('size',0)} bytes | "
            f"status: {f.get('change_type','unknown')} | "
            f"backed up: {age_days:.1f} days ago"
        )

    files_text = "\n".join(file_summaries)

    prompt = f"""You are an AI backup prioritization system.
Analyze these backed-up files and rank them by how urgently they should be backed up next.

Consider:
- Recently modified files are higher priority
- Important file types (code, documents, configs) are higher priority  
- Files with keywords like "final", "project", "important" are higher priority
- Larger files that changed recently are critical
- Log files and temp files are low priority

Files to analyze:
{files_text}

Respond ONLY with a valid JSON array. Each item must have:
- "path": the file path (exact match from input)
- "priority": "high", "medium", or "low"
- "score": number 0-100
- "reason": one short sentence explaining why

Example format:
[
  {{"path": "report.pdf", "priority": "high", "score": 90, "reason": "Recently modified critical document"}},
  {{"path": "notes.txt", "priority": "medium", "score": 50, "reason": "Text file modified a week ago"}}
]

Return ONLY the JSON array, nothing else."""

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=1024,
    )

    raw = response.choices[0].message.content.strip()

    # Parse the JSON response
    try:
        # Strip markdown code blocks if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        ranked = json.loads(raw.strip())
    except json.JSONDecodeError:
        # Fallback — return files unchanged with default scores
        return [{"path": f["path"], "priority": "medium", "score": 50, "reason": "AI ranking unavailable"} for f in files]

    # Merge AI ranking back into original file metadata
    ranked_dict = {r["path"]: r for r in ranked}
    result = []
    for f in files:
        ai_rank = ranked_dict.get(f["path"], {"priority": "medium", "score": 50, "reason": "Not ranked"})
        result.append({
            **f,
            "ai_priority":  ai_rank.get("priority", "medium"),
            "ai_score":     ai_rank.get("score", 50),
            "ai_reason":    ai_rank.get("reason", ""),
        })

    # Sort by AI score descending
    result.sort(key=lambda x: x.get("ai_score", 0), reverse=True)
    return result
