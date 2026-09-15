from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest
from test_foundation import NOW, book, market

from polyalpha.graph import ConstraintGraph, ReviewedNode
from polyalpha.parsing import parse_book, parse_market
from polyalpha.relative_value import Constraint
from polyalpha.resolution import ResolutionReview, definition_hash


def fixture():
    nodes, books = [], {}
    for i, ask in enumerate([".39", ".32", ".17", ".08"]):
        raw = market()
        raw.update(
            id=str(i), conditionId=f"c{i}", clobTokenIds=[f"n{i}", f"y{i}"], feesEnabled=False
        )
        m = parse_market(raw, NOW)
        review = ResolutionReview(
            definition_hash(m), NOW, "synthetic-definition-review", D(1), D(0), D(0)
        )
        nodes.append(ReviewedNode(m, review))
        raw_book = book(f"y{i}")
        raw_book.update(
            market=f"c{i}",
            bids=[dict(price=str(D(ask) - D(".01")), size="1000")],
            asks=[dict(price=ask, size="1000")],
        )
        books[f"y{i}"] = parse_book(raw_book, NOW)
    rule = Constraint(
        "partition", tuple(str(i) for i in range(4)), "synthetic-exhaustiveness-review", NOW
    )
    return ConstraintGraph(nodes, [rule]), rule, books


def test_partition_accounts_for_all_legs_and_buffers():
    graph, rule, books = fixture()
    report = graph.quote_partition(rule, books, D(100), NOW)
    assert report["execution_cost"] == "96.00000"
    assert D(report["net_surplus"]) == D("-.8")
    assert report["atomic_execution"] is False
    assert report["status"] == "research_quote"


def test_partial_basket_never_reports_surplus():
    graph, rule, books = fixture()
    report = graph.quote_partition(rule, books, D(1001), NOW)
    assert report["status"] == "rejected"
    assert report["net_surplus"] is None
    assert all("insufficient depth" in r for r in report["reasons"])


def test_unknown_fees_fail_closed():
    graph, rule, books = fixture()
    node = graph.nodes["0"]
    graph.nodes["0"] = replace(node, market=replace(node.market, fees_enabled=None))
    assert "0:unknown fees" in graph.quote_partition(rule, books, D(100), NOW)["reasons"]


def test_definition_change_invalidates_graph():
    graph, rule, books = fixture()
    node = graph.nodes["0"]
    graph.nodes["0"] = replace(node, market=replace(node.market, question="Different contract"))
    with pytest.raises(ValueError, match="definition changed"):
        graph.quote_partition(rule, books, D(100), NOW)


def test_future_review_and_skew_are_rejected():
    graph, rule, books = fixture()
    with pytest.raises(ValueError, match="unavailable constraint"):
        graph.quote_partition(rule, books, D(100), NOW - timedelta(seconds=1))
    books["y0"] = replace(books["y0"], source_at=NOW - timedelta(seconds=3))
    report = graph.quote_partition(rule, books, D(100), NOW)
    assert "asynchronous_books" in report["reasons"]
