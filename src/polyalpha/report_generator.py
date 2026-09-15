"""Automated report generation — JSON, CSV, terminal, and markdown.

Part 71: Assembles all EMPIRICAL_ALPHA_REPORT sections into a single
structured report. Supports multiple output formats.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class ReportSection:
    """A single section of the report."""

    title: str
    content: dict | list | str
    severity: str = "info"  # "info", "warning", "critical"


@dataclass
class EmpiricalAlphaReport:
    """Full empirical alpha report assembled from all analysis sections."""

    generated_at: str
    dataset_info: dict
    benchmark: dict | None = None
    ablation: dict | None = None
    cost_ladder: dict | None = None
    disagreement: dict | None = None
    categories: dict | None = None
    time_resolution: dict | None = None
    bootstrap: dict | None = None
    stress: dict | None = None
    autopsies: dict | None = None
    sections: list[ReportSection] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "section_count": len(self.sections),
            "has_benchmark": self.benchmark is not None,
            "has_ablation": self.ablation is not None,
            "has_cost_ladder": self.cost_ladder is not None,
            "has_disagreement": self.disagreement is not None,
            "has_categories": self.categories is not None,
            "has_time_resolution": self.time_resolution is not None,
            "has_bootstrap": self.bootstrap is not None,
            "has_stress": self.stress is not None,
            "has_autopsies": self.autopsies is not None,
        }


def _add_section(report: EmpiricalAlphaReport, title: str, data, severity: str = "info"):
    """Add a section to the report if data is present."""
    if data is not None:
        report.sections.append(ReportSection(title=title, content=data, severity=severity))


def _determine_overall_severity(report: EmpiricalAlphaReport) -> str:
    """Determine overall severity from sections."""
    severities = [s.severity for s in report.sections]
    if "critical" in severities:
        return "critical"
    if "warning" in severities:
        return "warning"
    return "info"


def generate_report(
    dataset_info: dict,
    benchmark: dict | None = None,
    ablation: dict | None = None,
    cost_ladder: dict | None = None,
    disagreement: dict | None = None,
    categories: dict | None = None,
    time_res: dict | None = None,
    bootstrap: dict | None = None,
    stress: dict | None = None,
    autopsies: dict | None = None,
    output_format: str = "json",
    output_path: str | None = None,
) -> str:
    """Generate a comprehensive empirical alpha report.

    Args:
        dataset_info: Dataset metadata and summary.
        benchmark: Model vs market benchmark results.
        ablation: Model ablation study results.
        cost_ladder: Cost ladder experiment results.
        disagreement: Disagreement analysis results.
        categories: Per-category analysis results.
        time_res: Time-to-resolution analysis results.
        bootstrap: Bootstrap confidence interval results.
        stress: Monte Carlo stress test results.
        autopsies: Negative control / autopsy results.
        output_format: "json", "csv", "terminal", or "markdown".
        output_path: Optional file path to write the report.

    Returns:
        Report content as a string.
    """
    report = EmpiricalAlphaReport(
        generated_at=datetime.now(UTC).isoformat(),
        dataset_info=dataset_info,
        benchmark=benchmark,
        ablation=ablation,
        cost_ladder=cost_ladder,
        disagreement=disagreement,
        categories=categories,
        time_resolution=time_res,
        bootstrap=bootstrap,
        stress=stress,
        autopsies=autopsies,
    )

    _add_section(report, "Dataset Overview", dataset_info)
    _add_section(report, "Market Benchmark", benchmark)
    _add_section(report, "Model Ablation", ablation)
    _add_section(report, "Cost Ladder", cost_ladder)
    _add_section(report, "Disagreement Analysis", disagreement)
    _add_section(report, "Category Performance", categories)
    _add_section(report, "Time to Resolution", time_res)
    _add_section(report, "Bootstrap Confidence Intervals", bootstrap)
    _add_section(report, "Stress Testing", stress)
    _add_section(report, "Negative Control Autopsies", autopsies)

    if output_format == "json":
        content = _render_json(report)
    elif output_format == "csv":
        content = _render_csv(report)
    elif output_format == "terminal":
        content = _render_terminal(report)
    elif output_format == "markdown":
        content = _render_markdown(report)
    else:
        raise ValueError(f"unknown format: {output_format!r}")

    if output_path:
        p = Path(output_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    return content


def _render_json(report: EmpiricalAlphaReport) -> str:
    """Render report as JSON."""
    data = {
        "generated_at": report.generated_at,
        "dataset": report.dataset_info,
        "sections": [
            {"title": s.title, "content": s.content, "severity": s.severity}
            for s in report.sections
        ],
        "overall_severity": _determine_overall_severity(report),
    }
    if report.benchmark:
        data["benchmark"] = report.benchmark
    if report.ablation:
        data["ablation"] = report.ablation
    if report.cost_ladder:
        data["cost_ladder"] = report.cost_ladder
    if report.disagreement:
        data["disagreement"] = report.disagreement
    if report.categories:
        data["categories"] = report.categories
    if report.time_resolution:
        data["time_resolution"] = report.time_resolution
    if report.bootstrap:
        data["bootstrap"] = report.bootstrap
    if report.stress:
        data["stress"] = report.stress
    if report.autopsies:
        data["autopsies"] = report.autopsies
    return json.dumps(data, indent=2, default=str)


def _render_csv(report: EmpiricalAlphaReport) -> str:
    """Render report as CSV (one row per section)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["section", "severity", "key", "value"])

    for section in report.sections:
        content = section.content
        if isinstance(content, dict):
            for k, v in content.items():
                writer.writerow([section.title, section.severity, k, str(v)])
        elif isinstance(content, list):
            for i, item in enumerate(content):
                writer.writerow([section.title, section.severity, f"[{i}]", str(item)])
        else:
            writer.writerow([section.title, section.severity, "", str(content)])

    return buf.getvalue()


def _render_terminal(report: EmpiricalAlphaReport) -> str:
    """Render report as terminal-friendly text."""
    lines = []
    lines.append("=" * 70)
    lines.append("EMPIRICAL ALPHA REPORT")
    lines.append(f"Generated: {report.generated_at}")
    lines.append(f"Overall severity: {_determine_overall_severity(report).upper()}")
    lines.append("=" * 70)

    for section in report.sections:
        lines.append("")
        lines.append(f"── {section.title} {'─' * max(0, 60 - len(section.title))}")
        if section.severity != "info":
            lines.append(f"  [{section.severity.upper()}]")
        content = section.content
        if isinstance(content, dict):
            for k, v in content.items():
                lines.append(f"  {k}: {v}")
        elif isinstance(content, list):
            for i, item in enumerate(content):
                lines.append(f"  [{i}] {item}")
        else:
            lines.append(f"  {content}")

    lines.append("")
    lines.append("=" * 70)
    return "\n".join(lines)


def _render_markdown(report: EmpiricalAlphaReport) -> str:
    """Render report as Markdown."""
    lines = []
    lines.append("# Empirical Alpha Report")
    lines.append("")
    lines.append(f"**Generated:** {report.generated_at}")
    lines.append(f"**Severity:** {_determine_overall_severity(report).upper()}")
    lines.append("")

    for section in report.sections:
        lines.append(f"## {section.title}")
        if section.severity != "info":
            lines.append(f"> **{section.severity.upper()}**")
            lines.append("")
        content = section.content
        if isinstance(content, dict):
            lines.append("| Key | Value |")
            lines.append("|-----|-------|")
            for k, v in content.items():
                lines.append(f"| {k} | {v} |")
        elif isinstance(content, list):
            for i, item in enumerate(content):
                lines.append(f"{i + 1}. {item}")
        else:
            lines.append(str(content))
        lines.append("")

    return "\n".join(lines)
