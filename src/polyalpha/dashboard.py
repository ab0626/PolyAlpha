"""Research Dashboard - Streamlit app for visualizing research results.

Usage: streamlit run src/polyalpha/dashboard.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_all_results(results_dir: str) -> dict[str, Any]:
    """Load JSON results from all research command output files."""
    results: dict[str, Any] = {}
    results_path = Path(results_dir)
    if not results_path.exists():
        return results

    for json_file in sorted(results_path.glob("*.json")):
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            key = json_file.stem
            results[key] = data
        except (json.JSONDecodeError, OSError):
            continue
    return results


def render_overview(results: dict[str, Any]) -> dict[str, Any]:
    """Prepare top-5 metrics: delta_brier, edge_realization, oos_pnl,
    cluster_adjusted_n, worst_drawdown."""
    metrics = {
        "delta_brier": results.get("delta_brier", None),
        "edge_realization": results.get("edge_realization", None),
        "oos_pnl": results.get("oos_pnl", None),
        "cluster_adjusted_n": results.get("cluster_adjusted_n", None),
        "worst_drawdown": results.get("worst_drawdown", None),
    }
    display = {}
    for name, val in metrics.items():
        if val is None:
            display[name] = {"value": "N/A", "status": "missing"}
        else:
            display[name] = {"value": val, "status": "loaded"}
    return {"metrics": display, "title": "Key Research Metrics"}


def render_model_vs_market(results: dict[str, Any]) -> dict[str, Any]:
    """Prepare heatmap data for model-vs-market probability comparison."""
    data = results.get("model_vs_market", {})
    categories = data.get("categories", [])
    model_probs = data.get("model_probs", [])
    market_probs = data.get("market_probs", [])
    deltas = data.get("deltas", [])

    return {
        "categories": categories,
        "model_probs": model_probs,
        "market_probs": market_probs,
        "deltas": deltas,
        "title": "Model vs Market Probability Heatmap",
    }


def render_calibration(results: dict[str, Any]) -> dict[str, Any]:
    """Prepare calibration chart data (predicted prob vs realized frequency)."""
    data = results.get("calibration", {})
    bins = data.get("bins", [])
    predicted = data.get("predicted_probs", [])
    realized = data.get("realized_freqs", [])
    counts = data.get("bin_counts", [])

    return {
        "bins": bins,
        "predicted": predicted,
        "realized": realized,
        "counts": counts,
        "title": "Calibration Plot",
        "brier_component": data.get("brier_component", None),
    }


def render_cost_ladder(results: dict[str, Any]) -> dict[str, Any]:
    """Prepare cost ladder bar chart data (edge at each cost tier)."""
    data = results.get("cost_ladder", {})
    tiers = data.get("tiers", [])
    edges = data.get("edges", [])
    cumulative_pnl = data.get("cumulative_pnl", [])

    return {
        "tiers": tiers,
        "edges": edges,
        "cumulative_pnl": cumulative_pnl,
        "title": "Cost Ladder Analysis",
        "breakeven_tier": data.get("breakeven_tier", None),
    }


def render_disagreement(results: dict[str, Any]) -> dict[str, Any]:
    """Prepare disagreement bucket chart data (model vs market divergence)."""
    data = results.get("disagreement", {})
    buckets = data.get("buckets", [])
    edge_by_bucket = data.get("edge_by_bucket", [])
    n_by_bucket = data.get("n_by_bucket", [])
    avg_outcome = data.get("avg_outcome_by_bucket", [])

    return {
        "buckets": buckets,
        "edge_by_bucket": edge_by_bucket,
        "n_by_bucket": n_by_bucket,
        "avg_outcome": avg_outcome,
        "title": "Disagreement Bucket Analysis",
    }


def render_ablations(results: dict[str, Any]) -> dict[str, Any]:
    """Prepare ablation comparison data (full model vs stripped variants)."""
    data = results.get("ablations", {})
    variants = data.get("variants", [])
    delta_brier_scores = data.get("delta_brier_scores", [])
    edge_scores = data.get("edge_scores", [])
    n_samples = data.get("n_samples", [])

    return {
        "variants": variants,
        "delta_brier": delta_brier_scores,
        "edge": edge_scores,
        "n_samples": n_samples,
        "title": "Model Ablation Comparison",
    }


def render_categories(results: dict[str, Any]) -> dict[str, Any]:
    """Prepare category performance data (per-category edge and volume)."""
    data = results.get("categories", {})
    names = data.get("names", [])
    edge_per_category = data.get("edge", [])
    volume_per_category = data.get("volume", [])
    brier_per_category = data.get("brier", [])

    return {
        "names": names,
        "edge": edge_per_category,
        "volume": volume_per_category,
        "brier": brier_per_category,
        "title": "Category Performance Breakdown",
    }


def render_time_to_resolution(results: dict[str, Any]) -> dict[str, Any]:
    """Prepare time horizon data (edge by days-to-resolution)."""
    data = results.get("time_to_resolution", {})
    horizons = data.get("horizons", [])
    edge_by_horizon = data.get("edge", [])
    n_by_horizon = data.get("n_samples", [])
    decay_rate = data.get("decay_rate", None)

    return {
        "horizons": horizons,
        "edge": edge_by_horizon,
        "n_samples": n_by_horizon,
        "decay_rate": decay_rate,
        "title": "Time-to-Resolution Edge Decay",
    }


def render_edge_decomposition(results: dict[str, Any]) -> dict[str, Any]:
    """Prepare edge waterfall data (components of total edge)."""
    data = results.get("edge_decomposition", {})
    components = data.get("components", [])
    values = data.get("values", [])
    total = data.get("total_edge", 0.0)

    return {
        "components": components,
        "values": values,
        "total_edge": total,
        "title": "Edge Decomposition Waterfall",
    }


def render_concentration(results: dict[str, Any]) -> dict[str, Any]:
    """Prepare concentration pie chart data (portfolio concentration)."""
    data = results.get("concentration", {})
    labels = data.get("labels", [])
    shares = data.get("shares", [])
    hhi = data.get("hhi", None)

    return {
        "labels": labels,
        "shares": shares,
        "hhi": hhi,
        "title": "Portfolio Concentration",
    }


def render_bootstrap(results: dict[str, Any]) -> dict[str, Any]:
    """Prepare bootstrap CI interval chart data."""
    data = results.get("bootstrap", {})
    metric_names = data.get("metrics", [])
    means = data.get("means", [])
    ci_lowers = data.get("ci_lowers", [])
    ci_uppers = data.get("ci_uppers", [])
    n_bootstrap = data.get("n_bootstrap", 0)

    return {
        "metrics": metric_names,
        "means": means,
        "ci_lowers": ci_lowers,
        "ci_uppers": ci_uppers,
        "n_bootstrap": n_bootstrap,
        "title": "Bootstrap Confidence Intervals",
    }


def render_monte_carlo(results: dict[str, Any]) -> dict[str, Any]:
    """Prepare Monte Carlo PnL distribution data."""
    data = results.get("monte_carlo", {})
    sim_pnl = data.get("simulated_pnl", [])
    percentiles = data.get("percentiles", {})
    var_95 = data.get("var_95", None)
    cvar_95 = data.get("cvar_95", None)

    return {
        "simulated_pnl": sim_pnl,
        "percentiles": percentiles,
        "var_95": var_95,
        "cvar_95": cvar_95,
        "title": "Monte Carlo PnL Distribution",
    }


def render_autopsies(results: dict[str, Any]) -> dict[str, Any]:
    """Prepare trade failure classification data."""
    data = results.get("autopsies", {})
    categories = data.get("failure_categories", [])
    counts = data.get("failure_counts", [])
    avg_loss = data.get("avg_loss_by_category", [])

    return {
        "failure_categories": categories,
        "failure_counts": counts,
        "avg_loss": avg_loss,
        "title": "Trade Autopsy Classification",
        "total_failures": data.get("total_failures", 0),
    }


def render_sensitivity(results: dict[str, Any]) -> dict[str, Any]:
    """Prepare parameter sensitivity heatmap data."""
    data = results.get("sensitivity", {})
    params = data.get("parameters", [])
    values = data.get("values", [])
    edge_matrix = data.get("edge_matrix", [])

    return {
        "parameters": params,
        "values": values,
        "edge_matrix": edge_matrix,
        "title": "Parameter Sensitivity Analysis",
    }


def render_regime(results: dict[str, Any]) -> dict[str, Any]:
    """Prepare regime performance comparison data."""
    data = results.get("regime", {})
    regimes = data.get("regimes", [])
    edge_by_regime = data.get("edge", [])
    sharpe_by_regime = data.get("sharpe", [])
    n_by_regime = data.get("n_samples", [])

    return {
        "regimes": regimes,
        "edge": edge_by_regime,
        "sharpe": sharpe_by_regime,
        "n_samples": n_by_regime,
        "title": "Regime Performance Comparison",
    }


def main():
    """Streamlit page config and layout."""
    import streamlit as st

    st.set_page_config(
        page_title="PolyAlpha Research Dashboard",
        page_icon="📊",
        layout="wide",
    )
    st.title("PolyAlpha Research Dashboard")

    results_dir = st.sidebar.text_input("Results directory", value="results/")
    results = load_all_results(results_dir)

    if not results:
        st.warning("No results found. Run research commands first.")
        return

    tab_names = [
        "Overview",
        "Model vs Market",
        "Calibration",
        "Cost Ladder",
        "Disagreement",
        "Ablations",
        "Categories",
        "Time-to-Resolution",
        "Edge Decomposition",
        "Concentration",
        "Bootstrap CIs",
        "Monte Carlo",
        "Autopsies",
        "Sensitivity",
        "Regime",
    ]
    tabs = st.tabs(tab_names)

    with tabs[0]:
        overview = render_overview(results)
        st.subheader(overview["title"])
        cols = st.columns(5)
        for i, (name, info) in enumerate(overview["metrics"].items()):
            with cols[i]:
                st.metric(label=name, value=str(info["value"]))

    with tabs[1]:
        mv = render_model_vs_market(results)
        st.subheader(mv["title"])
        if mv["categories"]:
            st.dataframe(
                {
                    "Category": mv["categories"],
                    "Model": mv["model_probs"],
                    "Market": mv["market_probs"],
                    "Delta": mv["deltas"],
                }
            )

    with tabs[2]:
        cal = render_calibration(results)
        st.subheader(cal["title"])
        if cal["bins"]:
            st.dataframe(
                {
                    "Predicted": cal["predicted"],
                    "Realized": cal["realized"],
                    "Count": cal["counts"],
                }
            )
            if cal["brier_component"] is not None:
                st.metric("Brier Component", f"{cal['brier_component']:.4f}")

    with tabs[3]:
        cl = render_cost_ladder(results)
        st.subheader(cl["title"])
        if cl["tiers"]:
            st.dataframe(
                {
                    "Tier": cl["tiers"],
                    "Edge": cl["edges"],
                    "Cumulative PnL": cl["cumulative_pnl"],
                }
            )
            if cl["breakeven_tier"] is not None:
                st.metric("Breakeven Tier", cl["breakeven_tier"])

    with tabs[4]:
        dis = render_disagreement(results)
        st.subheader(dis["title"])
        if dis["buckets"]:
            st.dataframe(
                {
                    "Bucket": dis["buckets"],
                    "Edge": dis["edge_by_bucket"],
                    "N": dis["n_by_bucket"],
                    "Avg Outcome": dis["avg_outcome"],
                }
            )

    with tabs[5]:
        ab = render_ablations(results)
        st.subheader(ab["title"])
        if ab["variants"]:
            st.dataframe(
                {
                    "Variant": ab["variants"],
                    "Delta Brier": ab["delta_brier"],
                    "Edge": ab["edge"],
                    "N": ab["n_samples"],
                }
            )

    with tabs[6]:
        cat = render_categories(results)
        st.subheader(cat["title"])
        if cat["names"]:
            st.dataframe(
                {
                    "Category": cat["names"],
                    "Edge": cat["edge"],
                    "Volume": cat["volume"],
                    "Brier": cat["brier"],
                }
            )

    with tabs[7]:
        ttr = render_time_to_resolution(results)
        st.subheader(ttr["title"])
        if ttr["horizons"]:
            st.dataframe(
                {
                    "Horizon (days)": ttr["horizons"],
                    "Edge": ttr["edge"],
                    "N": ttr["n_samples"],
                }
            )
            if ttr["decay_rate"] is not None:
                st.metric("Decay Rate", f"{ttr['decay_rate']:.4f}")

    with tabs[8]:
        ed = render_edge_decomposition(results)
        st.subheader(ed["title"])
        if ed["components"]:
            st.dataframe(
                {
                    "Component": ed["components"],
                    "Value": ed["values"],
                }
            )
            st.metric("Total Edge", f"{ed['total_edge']:.4f}")

    with tabs[9]:
        con = render_concentration(results)
        st.subheader(con["title"])
        if con["labels"]:
            st.dataframe(
                {
                    "Label": con["labels"],
                    "Share": con["shares"],
                }
            )
            if con["hhi"] is not None:
                st.metric("HHI", f"{con['hhi']:.4f}")

    with tabs[10]:
        boot = render_bootstrap(results)
        st.subheader(boot["title"])
        if boot["metrics"]:
            st.dataframe(
                {
                    "Metric": boot["metrics"],
                    "Mean": boot["means"],
                    "CI Lower": boot["ci_lowers"],
                    "CI Upper": boot["ci_uppers"],
                }
            )
            st.caption(f"Based on {boot['n_bootstrap']} bootstrap iterations")

    with tabs[11]:
        mc = render_monte_carlo(results)
        st.subheader(mc["title"])
        if mc["simulated_pnl"]:
            st.line_chart(mc["simulated_pnl"])
            if mc["var_95"] is not None:
                st.metric("VaR (95%)", f"{mc['var_95']:.2f}")
            if mc["cvar_95"] is not None:
                st.metric("CVaR (95%)", f"{mc['cvar_95']:.2f}")

    with tabs[12]:
        auto = render_autopsies(results)
        st.subheader(auto["title"])
        if auto["failure_categories"]:
            st.dataframe(
                {
                    "Category": auto["failure_categories"],
                    "Count": auto["failure_counts"],
                    "Avg Loss": auto["avg_loss"],
                }
            )
            st.metric("Total Failures", auto["total_failures"])

    with tabs[13]:
        sens = render_sensitivity(results)
        st.subheader(sens["title"])
        if sens["parameters"]:
            import pandas as pd

            matrix_df = pd.DataFrame(
                sens["edge_matrix"],
                index=sens["parameters"],
                columns=sens["values"],
            )
            st.dataframe(matrix_df)

    with tabs[14]:
        reg = render_regime(results)
        st.subheader(reg["title"])
        if reg["regimes"]:
            st.dataframe(
                {
                    "Regime": reg["regimes"],
                    "Edge": reg["edge"],
                    "Sharpe": reg["sharpe"],
                    "N": reg["n_samples"],
                }
            )


if __name__ == "__main__":
    main()
