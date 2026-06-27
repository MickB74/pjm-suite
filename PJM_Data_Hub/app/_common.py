"""Shared Streamlit helpers for the PJM Data Hub."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st


def data_status(st_obj, *, path: Path, rows: int, span: tuple) -> None:
    dmin, dmax = span
    st_obj.caption(
        f"**{rows:,} rows** · {dmin} → {dmax} · "
        f"last modified {pd.Timestamp(path.stat().st_mtime, unit='s').strftime('%Y-%m-%d %H:%M') if path.exists() else 'never'}"
    )


def empty_state(st_obj, title: str, hint: str = "", page: str = "", page_label: str = "") -> None:
    st_obj.warning(f"**{title}** {hint}")
    if page and page_label:
        if st_obj.button(page_label):
            st.switch_page(page)
    st_obj.stop()


def period_picker(
    st_obj,
    *,
    key: str,
    min_year: int = 2019,
    default_mode: str = "Month",
) -> tuple[date, date]:
    today = date.today()
    mode = st_obj.radio("Period", ["Month", "Year", "Custom"], index=["Month", "Year", "Custom"].index(default_mode),
                        horizontal=True, key=f"{key}_mode")
    if mode == "Month":
        col1, col2 = st_obj.columns(2)
        year = col1.selectbox("Year", range(today.year, min_year - 1, -1), key=f"{key}_yr")
        month = col2.selectbox("Month", range(1, 13), index=today.month - 2 if today.month > 1 else 0,
                               key=f"{key}_mo")
        start = date(year, month, 1)
        end = (pd.Timestamp(start) + pd.offsets.MonthEnd(0)).date()
        return start, end
    if mode == "Year":
        year = st_obj.selectbox("Year", range(today.year, min_year - 1, -1), key=f"{key}_yr_y")
        return date(year, 1, 1), date(year, 12, 31)
    # Custom
    col1, col2 = st_obj.columns(2)
    start = col1.date_input("Start", date(today.year, 1, 1), key=f"{key}_s")
    end = col2.date_input("End", today, key=f"{key}_e")
    return start, end
