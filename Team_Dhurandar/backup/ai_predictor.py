"""
ai_predictor.py
---------------
Plugs your scorer.py into the backup pipeline.

Flow:
  1. Each FileChange is converted into the dict format scorer.py expects
  2. calculate_priority() scores it (recency + type + size + keywords)
  3. Score is normalised 0–100 → 0.0–1.0
  4. ML layer (RandomForest) blends in after 3+ backup runs
  5. rank() returns files sorted highest score first

Usage:
    from backup.ai_predictor import AIPredictor

    predictor = AIPredictor(history_path=".backup/predictor.json")
    ranked    = predictor.rank(manifest, watch_root="test_folder")
    predictor.record(manifest)   # call after every successful backup
"""

import json
import math
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from backup.change_detector import FileChange, ChangeType
from backup.scorer import calculate_priority          # ← your scorer


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class FileHistory:
    """Tracks how often a file has changed across backup runs."""
    path:            str
    total_runs:      int   = 0
    change_count:    int   = 0
    last_changed_at: float = 0.0
    last_seen_at:    float = 0.0
    size_bytes:      int   = 0


@dataclass
class ScoredFile:
    path:           str
    change_type:    ChangeType
    score:          float        # 0.0 – 1.0  (normalised from scorer's 0–100)
    priority_label: str          # "high" / "medium" / "low" from scorer
    reason:         str          # score breakdown for transparency


# ── ML layer ──────────────────────────────────────────────────────────────────

class MLScorer:
    """
    RandomForest trained on backup history.
    Blends with scorer.py after MIN_RUNS runs.
    """
    MIN_RUNS = 3

    def __init__(self):
        self._model  = None
        self._fitted = False

    def _features(self, history: FileHistory) -> list[float]:
        halflife = 7 * 24 * 3600
        freq     = history.change_count / max(history.total_runs, 1)
        age      = time.time() - history.last_changed_at
        recency  = math.exp(-age / halflife)
        size_mb  = history.size_bytes / (1024 * 1024)
        runs_log = math.log1p(history.total_runs)
        return [freq, recency, size_mb, runs_log]

    def train(self, run_log: list[dict]):
        try:
            from sklearn.ensemble import RandomForestClassifier
        except ImportError:
            return
        if len(run_log) < self.MIN_RUNS * 2:
            return
        X, y = [], []
        for entry in run_log:
            h = FileHistory(**entry["history"])
            X.append(self._features(h))
            y.append(entry["changed"])
        if len(set(y)) < 2:
            return
        clf = RandomForestClassifier(n_estimators=50, random_state=42)
        clf.fit(X, y)
        self._model  = clf
        self._fitted = True

    def score(self, history: FileHistory) -> float | None:
        if not self._fitted or self._model is None:
            return None
        prob    = self._model.predict_proba([self._features(history)])[0]
        classes = list(self._model.classes_)
        return float(prob[classes.index(1)]) if 1 in classes else None


# ── AI Predictor ──────────────────────────────────────────────────────────────

class AIPredictor:
    """
    Combines your scorer.py with ML history to rank files.

    Args:
        history_path: Where to persist run history for ML training.
    """

    ML_BLEND = 0.4   # weight given to ML when available (60% scorer, 40% ML)

    def __init__(self, history_path: str | Path = ".backup/predictor.json"):
        self.history_path = Path(history_path)
        self._history: dict[str, FileHistory] = {}
        self._run_log: list[dict]             = []
        self._ml = MLScorer()
        self._load()
        self._ml.train(self._run_log)

    # ── Public API ─────────────────────────────────────────────────────────────

    def rank(self, manifest: list[FileChange], watch_root: str | Path) -> list[ScoredFile]:
        """
        Score and rank all files. Returns highest priority first.
        """
        scored = []
        root   = Path(watch_root).expanduser().resolve()

        for fc in manifest:
            if fc.change_type in (ChangeType.NEW, ChangeType.MODIFIED):
                abs_path = root / fc.path

                # Build the dict scorer.py expects
                file_dict = {
                    "file_path":           str(abs_path),
                    "file_name":           abs_path.name,
                    "file_size":           fc.size or 0,
                    "last_modified_time":  fc.mtime or time.time(),
                    "file_type":           abs_path.suffix.lower(),
                    "is_duplicate":        False,
                    "is_changed":          True,
                }

                # Score via your scorer.py
                result    = calculate_priority(file_dict)
                raw_score = result["priority_score"]    # 0–100
                label     = result["priority_label"]    # high/medium/low
                breakdown = result["score_breakdown"]

                # Normalise to 0.0–1.0
                base_score = raw_score / 100.0

                # Blend with ML if available
                history  = self._history.get(fc.path)
                ml_score = self._ml.score(history) if history else None
                if ml_score is not None:
                    final_score = (1 - self.ML_BLEND) * base_score + self.ML_BLEND * ml_score
                    reason = (
                        f"scorer={raw_score}/100 ({label}) | "
                        f"ML={ml_score:.2f} | "
                        f"recency={breakdown['recency_score']} "
                        f"type={breakdown['type_score']} "
                        f"size={breakdown['size_score']} "
                        f"keywords={breakdown['keyword_score']}"
                    )
                else:
                    final_score = base_score
                    reason = (
                        f"scorer={raw_score}/100 ({label}) | "
                        f"recency={breakdown['recency_score']} "
                        f"type={breakdown['type_score']} "
                        f"size={breakdown['size_score']} "
                        f"keywords={breakdown['keyword_score']}"
                    )

            else:
                final_score = 0.0
                label       = "low"
                reason      = fc.change_type.value

            scored.append(ScoredFile(
                path=fc.path,
                change_type=fc.change_type,
                score=round(final_score, 4),
                priority_label=label,
                reason=reason,
            ))

        scored.sort(key=lambda s: s.score, reverse=True)
        return scored

    def record(self, manifest: list[FileChange]):
        """Update history after a successful backup run."""
        now = time.time()
        for fc in manifest:
            h       = self._history.get(fc.path) or FileHistory(path=fc.path)
            changed = fc.change_type in (ChangeType.NEW, ChangeType.MODIFIED)
            h.total_runs  += 1
            h.last_seen_at = now
            if changed:
                h.change_count   += 1
                h.last_changed_at = now
            if fc.size:
                h.size_bytes = fc.size
            self._history[fc.path] = h
            self._run_log.append({
                "changed":   int(changed),
                "timestamp": now,
                "history":   asdict(h),
            })
        self._run_log = self._run_log[-2000:]
        self._save()
        self._ml.train(self._run_log)

    def top_files(self, n: int = 10) -> list[tuple[str, str, float]]:
        """Return top N files most likely to need backing up next."""
        results = []
        for path, history in self._history.items():
            file_dict = {
                "file_path":          path,
                "file_name":          Path(path).name,
                "file_size":          history.size_bytes,
                "last_modified_time": history.last_changed_at or time.time(),
                "file_type":          Path(path).suffix.lower(),
                "is_duplicate":       False,
                "is_changed":         True,
            }
            result    = calculate_priority(file_dict)
            raw_score = result["priority_score"] / 100.0
            label     = result["priority_label"]
            ml_score  = self._ml.score(history)
            score = (
                (1 - self.ML_BLEND) * raw_score + self.ML_BLEND * ml_score
                if ml_score is not None else raw_score
            )
            results.append((path, label, round(score, 4)))

        results.sort(key=lambda x: x[2], reverse=True)
        return results[:n]

    # ── Persistence ────────────────────────────────────────────────────────────

    def _load(self):
        if not self.history_path.exists():
            return
        data = json.loads(self.history_path.read_text())
        self._history = {k: FileHistory(**v) for k, v in data.get("history", {}).items()}
        self._run_log = data.get("run_log", [])

    def _save(self):
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        self.history_path.write_text(json.dumps({
            "history": {k: asdict(v) for k, v in self._history.items()},
            "run_log": self._run_log,
        }, indent=2))
