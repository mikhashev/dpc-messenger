"""
Effectiveness Tracker - Phase 6

Tracks usage and effectiveness of knowledge commits over time.
Implements spaced repetition scheduling and suggests improvements
for low-performing knowledge.
"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta

from dpc_protocol.crypto import DPC_HOME_DIR

logger = logging.getLogger(__name__)


@dataclass
class CommitMetrics:
    """Tracks effectiveness metrics for a single commit"""

    commit_id: str

    # Usage tracking
    times_referenced: int = 0
    times_edited: int = 0
    last_accessed: Optional[str] = None

    # User feedback
    helpful_count: int = 0
    unhelpful_count: int = 0
    effectiveness_score: float = 0.0  # Calculated from feedback

    # Quality indicators
    confidence_at_commit: float = 1.0
    confidence_after_usage: float = 1.0  # Adjusted based on edits/feedback
    cultural_applicability: Dict[str, int] = field(default_factory=dict)  # {"Western": 5, "Eastern": 3}

    # Spaced repetition
    review_interval_days: int = 1  # Days until next review
    next_review_date: Optional[str] = None
    review_count: int = 0

    # Improvement suggestions
    needs_revision: bool = False
    revision_reason: Optional[str] = None

    def update_effectiveness(self):
        """Recalculate effectiveness score"""
        total_feedback = self.helpful_count + self.unhelpful_count
        if total_feedback > 0:
            self.effectiveness_score = self.helpful_count / total_feedback

        # Adjust confidence based on edits (edits suggest knowledge wasn't quite right)
        if self.times_edited > 0:
            penalty = min(0.1 * self.times_edited, 0.5)  # Max 50% penalty
            self.confidence_after_usage = max(0.1, self.confidence_at_commit - penalty)

    def schedule_next_review(self):
        """Schedule next review using spaced repetition algorithm

        Uses simplified SM-2 algorithm:
        - If recalled successfully: interval *= 2
        - If failed: interval = 1 day
        """
        if self.effectiveness_score >= 0.7:
            # Good recall - increase interval
            self.review_interval_days = min(self.review_interval_days * 2, 180)  # Max 6 months
        else:
            # Poor recall - reset interval
            self.review_interval_days = 1

        self.next_review_date = (
            datetime.now(timezone.utc) + timedelta(days=self.review_interval_days)
        ).isoformat()
        self.review_count += 1


class EffectivenessTracker:
    """Tracks and manages effectiveness metrics for knowledge commits"""

    def __init__(self, storage_dir: Optional[Path] = None):
        """Initialize tracker

        Args:
            storage_dir: Directory to store metrics (default: ~/.dpc/metrics/)
        """
        if storage_dir:
            self.storage_dir = Path(storage_dir)
        else:
            self.storage_dir = DPC_HOME_DIR / "metrics"

        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.metrics_file = self.storage_dir / "commit_metrics.json"

        # Load existing metrics
        self.metrics: Dict[str, CommitMetrics] = self._load_metrics()

    def _load_metrics(self) -> Dict[str, CommitMetrics]:
        """Load metrics from storage

        Returns:
            Dictionary mapping commit_id to CommitMetrics
        """
        if not self.metrics_file.exists():
            return {}

        try:
            with open(self.metrics_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            metrics = {}
            for commit_id, metric_data in data.items():
                metrics[commit_id] = CommitMetrics(**metric_data)

            return metrics
        except Exception as e:
            logger.error("Error loading metrics: %s", e, exc_info=True)
            return {}

    def _save_metrics(self):
        """Save metrics to storage"""
        try:
            data = {
                commit_id: asdict(metrics)
                for commit_id, metrics in self.metrics.items()
            }

            with open(self.metrics_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

        except Exception as e:
            logger.error("Error saving metrics: %s", e, exc_info=True)

    def track_reference(self, commit_id: str):
        """Track that a commit was referenced

        Args:
            commit_id: Commit identifier
        """
        if commit_id not in self.metrics:
            self.metrics[commit_id] = CommitMetrics(commit_id=commit_id)

        metrics = self.metrics[commit_id]
        metrics.times_referenced += 1
        metrics.last_accessed = datetime.now(timezone.utc).isoformat()

        self._save_metrics()

    def track_edit(self, commit_id: str):
        """Track that a commit's knowledge was edited

        Args:
            commit_id: Commit identifier
        """
        if commit_id not in self.metrics:
            self.metrics[commit_id] = CommitMetrics(commit_id=commit_id)

        metrics = self.metrics[commit_id]
        metrics.times_edited += 1
        metrics.update_effectiveness()

        # Flag for revision if edited too many times
        if metrics.times_edited >= 3:
            metrics.needs_revision = True
            metrics.revision_reason = f"Edited {metrics.times_edited} times - may need improvement"

        self._save_metrics()

    def record_feedback(
        self,
        commit_id: str,
        helpful: bool,
        cultural_context: Optional[str] = None
    ):
        """Record user feedback on commit helpfulness

        Args:
            commit_id: Commit identifier
            helpful: True if helpful, False if not
            cultural_context: Optional cultural context (e.g., "Western", "Eastern")
        """
        if commit_id not in self.metrics:
            self.metrics[commit_id] = CommitMetrics(commit_id=commit_id)

        metrics = self.metrics[commit_id]

        if helpful:
            metrics.helpful_count += 1
        else:
            metrics.unhelpful_count += 1

        # Track cultural applicability
        if cultural_context and helpful:
            if cultural_context not in metrics.cultural_applicability:
                metrics.cultural_applicability[cultural_context] = 0
            metrics.cultural_applicability[cultural_context] += 1

        metrics.update_effectiveness()

        # Flag for revision if effectiveness is low
        total_feedback = metrics.helpful_count + metrics.unhelpful_count
        if total_feedback >= 5 and metrics.effectiveness_score < 0.5:
            metrics.needs_revision = True
            metrics.revision_reason = f"Low effectiveness score: {metrics.effectiveness_score:.0%}"

        self._save_metrics()

    def schedule_review(self, commit_id: str, success: bool):
        """Schedule next review for spaced repetition

        Args:
            commit_id: Commit identifier
            success: True if recall was successful, False otherwise
        """
        if commit_id not in self.metrics:
            self.metrics[commit_id] = CommitMetrics(commit_id=commit_id)

        metrics = self.metrics[commit_id]

        # Update effectiveness based on recall success
        if success:
            metrics.helpful_count += 1
        else:
            metrics.unhelpful_count += 1

        metrics.update_effectiveness()
        metrics.schedule_next_review()

        self._save_metrics()

    def get_commits_needing_review(self) -> List[Tuple[str, CommitMetrics]]:
        """Get commits that need review today

        Returns:
            List of (commit_id, metrics) tuples
        """
        now = datetime.now(timezone.utc).isoformat()
        needing_review = []

        for commit_id, metrics in self.metrics.items():
            if metrics.next_review_date and metrics.next_review_date <= now:
                needing_review.append((commit_id, metrics))

        # Sort by review date (oldest first)
        needing_review.sort(key=lambda x: x[1].next_review_date or "")

        return needing_review

    def get_commits_needing_revision(self) -> List[Tuple[str, CommitMetrics]]:
        """Get commits flagged for revision

        Returns:
            List of (commit_id, metrics) tuples
        """
        needing_revision = [
            (commit_id, metrics)
            for commit_id, metrics in self.metrics.items()
            if metrics.needs_revision
        ]

        # Sort by effectiveness score (worst first)
        needing_revision.sort(key=lambda x: x[1].effectiveness_score)

        return needing_revision

    def get_commit_stats(self, commit_id: str) -> Optional[CommitMetrics]:
        """Get metrics for specific commit

        Args:
            commit_id: Commit identifier

        Returns:
            CommitMetrics or None
        """
        return self.metrics.get(commit_id)

    def get_overall_stats(self) -> Dict[str, any]:
        """Get overall statistics across all commits

        Returns:
            Dictionary with aggregate stats
        """
        if not self.metrics:
            return {
                'total_commits': 0,
                'avg_effectiveness': 0.0,
                'total_references': 0,
                'commits_needing_review': 0,
                'commits_needing_revision': 0
            }

        total_effectiveness = sum(m.effectiveness_score for m in self.metrics.values())
        total_references = sum(m.times_referenced for m in self.metrics.values())

        return {
            'total_commits': len(self.metrics),
            'avg_effectiveness': total_effectiveness / len(self.metrics),
            'total_references': total_references,
            'commits_needing_review': len(self.get_commits_needing_review()),
            'commits_needing_revision': len(self.get_commits_needing_revision())
        }

    def generate_improvement_suggestions(
        self,
        commit_id: str
    ) -> List[str]:
        """Generate suggestions for improving a commit

        Args:
            commit_id: Commit identifier

        Returns:
            List of suggestion strings
        """
        metrics = self.metrics.get(commit_id)
        if not metrics:
            return []

        suggestions = []

        # Low effectiveness
        if metrics.effectiveness_score < 0.5 and (metrics.helpful_count + metrics.unhelpful_count) >= 3:
            suggestions.append(
                f"Low effectiveness ({metrics.effectiveness_score:.0%}). "
                "Consider revising content or adding more examples."
            )

        # Many edits
        if metrics.times_edited >= 3:
            suggestions.append(
                f"Edited {metrics.times_edited} times. "
                "Content may be incomplete or unclear. Consider comprehensive rewrite."
            )

        # Low confidence
        if metrics.confidence_after_usage < 0.7:
            suggestions.append(
                f"Confidence dropped to {metrics.confidence_after_usage:.0%}. "
                "Verify accuracy and add citations."
            )

        # Rarely used
        if metrics.times_referenced == 0 and metrics.review_count > 3:
            suggestions.append(
                "Never referenced despite multiple reviews. Consider if this knowledge is relevant."
            )

        # Cultural bias
        if metrics.cultural_applicability:
            contexts = list(metrics.cultural_applicability.keys())
            if len(contexts) == 1:
                suggestions.append(
                    f"Only validated in {contexts[0]} context. "
                    "Test applicability in other cultural contexts."
                )

        return suggestions


# Example usage
if __name__ == '__main__':
    print("=== EffectivenessTracker Demo ===\n")

    # Create tracker
    tracker = EffectivenessTracker()

    commit_id = "commit-abc123"

    print("1. Tracking usage:")
    tracker.track_reference(commit_id)
    tracker.track_reference(commit_id)
    tracker.track_reference(commit_id)
    print(f"   Referenced {tracker.get_commit_stats(commit_id).times_referenced} times")
    print()

    print("2. Recording feedback:")
    tracker.record_feedback(commit_id, helpful=True, cultural_context="Western")
    tracker.record_feedback(commit_id, helpful=True, cultural_context="Western")
    tracker.record_feedback(commit_id, helpful=False, cultural_context="Eastern")
    metrics = tracker.get_commit_stats(commit_id)
    print(f"   Helpful: {metrics.helpful_count}")
    print(f"   Unhelpful: {metrics.unhelpful_count}")
    print(f"   Effectiveness: {metrics.effectiveness_score:.0%}")
    print()

    print("3. Scheduling reviews:")
    tracker.schedule_review(commit_id, success=True)
    metrics = tracker.get_commit_stats(commit_id)
    print(f"   Next review: {metrics.next_review_date}")
    print(f"   Interval: {metrics.review_interval_days} days")
    print()

    print("4. Tracking edits:")
    tracker.track_edit(commit_id)
    tracker.track_edit(commit_id)
    tracker.track_edit(commit_id)
    metrics = tracker.get_commit_stats(commit_id)
    print(f"   Edited: {metrics.times_edited} times")
    print(f"   Confidence: {metrics.confidence_after_usage:.0%}")
    print(f"   Needs revision: {metrics.needs_revision}")
    print()

    print("5. Improvement suggestions:")
    suggestions = tracker.generate_improvement_suggestions(commit_id)
    for i, suggestion in enumerate(suggestions, 1):
        print(f"   {i}. {suggestion}")
    print()

    print("6. Overall stats:")
    stats = tracker.get_overall_stats()
    for key, value in stats.items():
        if isinstance(value, float):
            print(f"   {key}: {value:.2f}")
        else:
            print(f"   {key}: {value}")

    print("\n=== Demo Complete ===")
