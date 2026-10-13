"""S12 reproducible analysis over a validated ``export-phase2`` bundle.

Spec: ``specs/session-12-phase25-release.md``. Input is the bundle only,
never SQLite, a pseudonym secret or a keyfile::

    export-phase2 bundle ─► bundle.load_phase2_bundle ─► ValidatedStudyBundle
                                                       ─► summary.build_summaries
                                                       ─► figures.FIGURES (S12b)
    report.publish_report: all of the above ─► one atomically published directory

No ``web``, ``db`` or exporter import: ``schema`` holds a frozen copy of the
``phase2_export_v1`` contract.
"""

from ehr_simulator.analysis.bundle import ConfigVersion, ValidatedStudyBundle, load_phase2_bundle
from ehr_simulator.analysis.errors import AnalysisError

__all__ = ["AnalysisError", "ConfigVersion", "ValidatedStudyBundle", "load_phase2_bundle"]
