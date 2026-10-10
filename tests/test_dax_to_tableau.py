"""Milestone 9: the DAX subset translates; everything else refuses loudly."""
from pathlib import Path

import pytest

from src.parsers.model_reader import load_dmv_dir, parse_dmv
from src.translators.dax_to_tableau import UnsupportedDax, translate, translate_measures

FIXTURES = Path(__file__).parent / "fixtures" / "dmv"


def name(table, col):
    return f"[{table}_{col}]"


MEASURES = {"Revenue Won": "Opportunities", "Revenue In Pipeline": "Opportunities", "Rev Goal": "Owners"}


@pytest.mark.parametrize("dax, expected", [
    ("SUM(Sales[Amount])", "SUM([Sales_Amount])"),
    ("AVERAGE('Sales Table'[Amount])", "AVG([Sales Table_Amount])"),
    ("DISTINCTCOUNT(Store[Id])", "COUNTD([Store_Id])"),
    ("[Revenue Won]+[Revenue In Pipeline]", "[Opportunities_Revenue Won] + [Opportunities_Revenue In Pipeline]"),
    ("(([Revenue Won]+[Revenue In Pipeline]))/ [Rev Goal]",
     "(([Opportunities_Revenue Won] + [Opportunities_Revenue In Pipeline])) / [Owners_Rev Goal]"),
    ("DIVIDE([Revenue Won], [Rev Goal])", "([Opportunities_Revenue Won] / [Owners_Rev Goal])"),
    ("DIVIDE([Revenue Won], [Rev Goal], 0)", "IFNULL([Opportunities_Revenue Won] / [Owners_Rev Goal], 0)"),
    ("IF([Rev Goal] > 0, [Revenue Won], BLANK())",
     "IF [Owners_Rev Goal] > 0 THEN [Opportunities_Revenue Won] ELSE NULL END"),
    ('IF(Sales[Status] = "Won" && Sales[Value] <> 0, 1, 0)',
     "IF ([Sales_Status] = 'Won' AND [Sales_Value] != 0) THEN 1 ELSE 0 END"),
    ("-SUM(Sales[Amount]) * 2 -- a comment", "-SUM([Sales_Amount]) * 2"),
    ("ABS(SUM(Sales[Amount]))", "ABS(SUM([Sales_Amount]))"),
])
def test_supported_subset(dax, expected):
    assert translate(dax, name, MEASURES) == expected


@pytest.mark.parametrize("dax, needle", [
    ("CALCULATE(SUM(Sales[Amount]), Sales[Status] = \"Won\")", "CALCULATE"),
    ("SUMX(Sales, Sales[Qty] * Sales[Price])", "SUMX"),
    ("VAR x = 1 RETURN x", "VAR"),
    ("[Unknown Measure] + 1", "not in the model"),
    ("SUM(1 + 2)", "over anything but a column"),
    ("SUM(Sales[Amount]", r"expected '\)'"),
])
def test_unsupported_raises_with_reason(dax, needle):
    with pytest.raises(UnsupportedDax, match=needle):
        translate(dax, name, MEASURES)


def test_real_regional_measures_split_honestly():
    model = parse_dmv(load_dmv_dir(FIXTURES))
    results = translate_measures(model.measures, name)
    ok = {t.measure.name for t in results if t.ok}
    failed = {t.measure.name: t.error for t in results if not t.ok}
    # simple arithmetic over other measures translates...
    assert {"Forecast", "Forecast %", "Close %", "Forecast by Win/Loss Ratio"} <= ok
    # ...CALCULATE / iterators / VAR do not, and say why
    assert "Revenue Won" in failed and "CALCULATE" in failed["Revenue Won"]
    assert "Revenue Won Average Deal Size" in failed and "AVERAGEX" in failed["Revenue Won Average Deal Size"]
    assert len(results) == 15
