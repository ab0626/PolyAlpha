"""Paper-trading research dashboard.

Usage: streamlit run src/polyalpha/dashboard/app.py

Pages:
1. Market Scanner - bid/ask/spread/model probability/net edge
2. Positions - cost/value/PnL/event cluster
3. Performance - equity curve/drawdown/rolling PnL
4. Forecast Quality - calibration curve/Brier/log loss
5. Signal Analysis - predicted vs realized edge/alpha decay
6. Risk - gross/cluster/category exposure/drawdown
7. Category Performance - fills/fees/slippage by category
8. Constraint Analysis - forecast distribution statistics
"""

import json
from pathlib import Path


def load_report(path: str) -> dict:
    """Load a backtest/paper report JSON file."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def run_dashboard(report_path: str | None = None):
    """Launch the Streamlit dashboard."""
    try:
        import streamlit as st
    except ImportError:
        print("Streamlit is required: pip install streamlit")
        print("Then run: streamlit run src/polyalpha/dashboard/app.py")
        return

    st.set_page_config(page_title="Polyalpha Research Dashboard", layout="wide")
    st.title("Polyalpha Research Dashboard")

    # Sidebar: load report
    st.sidebar.header("Report")
    if report_path:
        path = report_path
    else:
        path = st.sidebar.text_input("Report JSON path", "data/paper/")

    if path and Path(path).exists():
        report = load_report(path)
    else:
        st.info("Enter a valid report JSON path in the sidebar.")
        return

    # Tab navigation
    tabs = st.tabs(
        [
            "Market Scanner",
            "Positions",
            "Performance",
            "Forecast Quality",
            "Signal Analysis",
            "Risk",
            "Category Performance",
            "Constraint Analysis",
        ]
    )

    with tabs[0]:
        st.header("Market Scanner")
        decisions = report.get("decisions", [])
        queued = [d for d in decisions if d.get("reason") == "queued"]
        filled = [d for d in decisions if d.get("reason") == "filled"]
        rejected = [d for d in decisions if d.get("reason") == "rejected"]

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total Signals", len(queued))
        col2.metric("Filled", len(filled))
        col3.metric("Rejected", len(rejected))
        col4.metric(
            "Fill Rate",
            f"{len(filled) / len(queued):.1%}" if queued else "N/A",
        )

        if queued:
            st.subheader("Recent Signals")
            st.dataframe(
                [
                    {
                        "Time": d.get("timestamp", "")[:19],
                        "Token": d.get("token", "")[:12],
                        "Fair P": d.get("fair_probability", ""),
                        "Conservative P": d.get("conservative_probability", ""),
                        "Best Ask": d.get("best_ask", ""),
                        "VWAP": d.get("vwap", ""),
                        "Net Edge": d.get("net_edge", ""),
                        "Spread": d.get("spread", ""),
                    }
                    for d in queued[-20:]
                ]
            )

    with tabs[1]:
        st.header("Positions")
        positions = report.get("positions", [])
        active = [p for p in positions if float(p.get("shares", 0)) > 0]

        col1, col2, col3 = st.columns(3)
        col1.metric("Active Positions", len(active))
        col2.metric(
            "Total Basis",
            f"${sum(float(p.get('basis', 0)) for p in active):.2f}",
        )
        col3.metric("Cash", report.get("cash", "N/A"))

        if active:
            st.dataframe(
                [
                    {
                        "Token": p.get("token_id", "")[:12],
                        "Shares": p.get("shares", 0),
                        "Avg Cost": p.get("average_cost", ""),
                        "Basis": p.get("basis", ""),
                        "Cluster": p.get("cluster", ""),
                        "Category": p.get("category", ""),
                    }
                    for p in active
                ]
            )

    with tabs[2]:
        st.header("Performance")
        equity = report.get("equity", [])
        complete_equity = [e for e in equity if not e.get("unliquidated")]

        if complete_equity:
            values = [float(e["equity"]) for e in complete_equity]

            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Starting Equity", f"${values[0]:.2f}")
            col2.metric("Final Equity", f"${values[-1]:.2f}")
            col3.metric(
                "Return",
                f"{(values[-1] / values[0] - 1):.2%}" if values[0] > 0 else "N/A",
            )
            col4.metric("Max Drawdown", report.get("max_drawdown", "N/A"))

            st.line_chart({e["timestamp"][:19]: float(e["equity"]) for e in complete_equity})
        else:
            st.info("No complete equity data available.")

        col1, col2, col3 = st.columns(3)
        col1.metric("Fills", report.get("fills", 0))
        col2.metric("Fees", report.get("fees", "0"))
        col3.metric("Realized PnL", report.get("realized_pnl", "N/A"))

    with tabs[3]:
        st.header("Forecast Quality")
        cal = report.get("calibration")
        if cal:
            col1, col2, col3 = st.columns(3)
            col1.metric("Brier Score", f"{cal['brier']:.4f}")
            col2.metric("Log Loss", f"{cal['log_loss']:.4f}")
            col3.metric("ECE", f"{cal['ece']:.4f}")

            st.subheader("Reliability Diagram")
            reliability = cal.get("reliability", [])
            if reliability:
                st.dataframe(
                    [
                        {
                            "Bucket": f"{r['lower']:.0%}-{r['upper']:.0%}",
                            "Count": r["count"],
                            "Predicted": f"{r['predicted']:.3f}" if r["predicted"] else "N/A",
                            "Observed": f"{r['observed']:.3f}" if r["observed"] else "N/A",
                        }
                        for r in reliability
                        if r["count"] > 0
                    ]
                )

            st.metric("Resolved Markets", report.get("resolved_markets", 0))
            st.metric("Independent Clusters", cal.get("independent_clusters", "N/A"))
        else:
            st.info("No calibration data available (markets may not have resolved yet).")

    with tabs[4]:
        st.header("Signal Analysis")
        decisions = report.get("decisions", [])
        filled = [d for d in decisions if d.get("reason") == "filled"]

        if filled:
            edges = [float(d.get("net_edge", 0)) for d in filled]
            st.metric("Average Realized Edge", f"{sum(edges) / len(edges):.4f}")
            st.metric("Total Realized Edge", f"{sum(edges):.4f}")

            st.subheader("Edge Distribution")
            st.bar_chart(edges)
        else:
            st.info("No filled trades to analyze.")

    with tabs[5]:
        st.header("Risk")
        risk = report.get("risk_state", {})
        col1, col2 = st.columns(2)
        col1.metric("Risk Halted", "YES" if risk.get("halted") else "NO")
        col2.metric("Halt Reason", risk.get("reason", "N/A"))

        cluster_exp = report.get("cluster_exposure", {})
        if cluster_exp:
            st.subheader("Cluster Exposure")
            st.bar_chart({k: float(v) for k, v in cluster_exp.items()})

        st.metric("Valuation Gaps", report.get("valuation_gap_count", 0))
        st.metric("Pending Unfilled", report.get("pending_unfilled", 0))

    with tabs[6]:
        st.header("Category Performance")
        cat_perf = report.get("category_performance", {})
        cat_exp = report.get("category_exposure", {})

        if cat_perf:
            st.subheader("Fills by Category")
            st.dataframe(
                [
                    {
                        "Category": cat,
                        "Fills": d["fills"],
                        "Notional": d["total_notional"],
                        "Fees": d["total_fees"],
                        "Slippage": d["total_slippage"],
                        "Unique Tokens": d["unique_tokens"],
                    }
                    for cat, d in cat_perf.items()
                ]
            )

            st.subheader("Fee Distribution by Category")
            st.bar_chart({cat: float(d["total_fees"]) for cat, d in cat_perf.items()})

        if cat_exp:
            st.subheader("Current Category Exposure")
            st.bar_chart({k: float(v) for k, v in cat_exp.items()})
        elif not cat_perf:
            st.info("No category performance data available yet.")

    with tabs[7]:
        st.header("Constraint Analysis")
        decisions = report.get("decisions", [])
        forecasts = report.get("forecasts", [])

        if forecasts:
            st.subheader("Forecast Distribution")
            probs = [float(f.get("p", 0)) for f in forecasts if "p" in f]
            if probs:
                st.bar_chart(probs)
                col1, col2, col3 = st.columns(3)
                col1.metric("Mean Forecast", f"{sum(probs) / len(probs):.3f}")
                col2.metric("Min Forecast", f"{min(probs):.3f}")
                col3.metric("Max Forecast", f"{max(probs):.3f}")
        else:
            st.info("No forecast data available.")


if __name__ == "__main__":
    run_dashboard()
