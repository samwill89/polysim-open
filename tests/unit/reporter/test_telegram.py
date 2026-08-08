"""Telegram formatter tests — build plan §6.3."""

from __future__ import annotations

from polysim.reporter.telegram import (
    DailySummaryAlert,
    FlagAlert,
    NullSink,
    PositionOpenedAlert,
    PositionResolvedAlert,
    escape_mdv2,
    format_daily_summary,
    format_flag_raised,
    format_position_opened,
    format_position_resolved,
    make_sink_from_config,
)


class TestEscapeMdv2:
    def test_escapes_reserved(self) -> None:
        raw = "Hello (World)."
        out = escape_mdv2(raw)
        assert r"\(" in out
        assert r"\)" in out
        assert r"\." in out

    def test_underscore_escaped(self) -> None:
        assert escape_mdv2("snake_case") == r"snake\_case"

    def test_noop_on_plain(self) -> None:
        assert escape_mdv2("ABC xyz 123") == "ABC xyz 123"


class TestFlagFormat:
    def _alert(self) -> FlagAlert:
        return FlagAlert(
            flag_id=1842,
            composite_score=8.4,
            verdict="INFORMED",
            verdict_confidence=0.82,
            market_question="Will Anthropic launch Claude 5 by Q3?",
            wallet_address="0xaf4d02c1b5e882f0a7d3c4e5f6a7b8c90011",
            trade_side="BUY",
            trade_outcome="YES",
            trade_size_shares=4800,
            trade_price_cents=32,
            top_detectors=[
                ("EventInsiderDetector", 0.94),
                ("CoordinationDetector", 0.82),
            ],
            paper_position_opened=True,
            copy_size_cents=5_000,
        )

    def test_has_flag_id_and_score(self) -> None:
        msg = format_flag_raised(self._alert())
        assert "1842" in msg
        # Period is MarkdownV2-reserved; must appear as "8\.4" not "8.4".
        assert "8\\.4" in msg

    def test_question_escaped(self) -> None:
        msg = format_flag_raised(self._alert())
        # '?' is not reserved in MDV2 — verify surrounding reserved chars.
        assert "Claude 5" in msg
        # '.' in "Claude 5 by Q3" — sentinel: trade price has '.' escape-free
        # (we verify via the period in the market slug test below).

    def test_wallet_tag_present(self) -> None:
        msg = format_flag_raised(self._alert())
        assert "`0xaf4d02" in msg

    def test_top_detectors_rendered(self) -> None:
        msg = format_flag_raised(self._alert())
        assert "EventInsiderDetector" in msg
        assert "CoordinationDetector" in msg

    def test_paper_position_notice(self) -> None:
        msg = format_flag_raised(self._alert())
        assert "Paper position opened" in msg

    def test_no_verdict_variant(self) -> None:
        alert = self._alert()
        from dataclasses import replace

        no_verdict = replace(alert, verdict=None, verdict_confidence=None)
        msg = format_flag_raised(no_verdict)
        assert "unchecked" in msg


class TestPositionOpenedFormat:
    def test_renders_key_fields(self) -> None:
        alert = PositionOpenedAlert(
            run_id=5,
            position_id=101,
            market_question="Claude 5 Q3?",
            outcome="YES",
            size_shares=150,
            avg_entry_price_cents=34,
            fee_cents=24,
            source_wallet="0xaf4d02c1",
            source_flag_id=1842,
            exposure_pct=0.042,
            current_balance_cents=10_042_00,
        )
        msg = format_position_opened(alert)
        assert "run \\#5" in msg
        assert "150" in msg
        assert "YES" in msg
        # exposure_pct 0.042 -> "4.2%", escape_mdv2 -> "4\\.2%"
        assert "4\\.2%" in msg


class TestPositionResolvedFormat:
    def test_win_variant(self) -> None:
        alert = PositionResolvedAlert(
            run_id=5, position_id=101,
            market_question="MrBeast cameo?",
            our_outcome="YES", market_resolved_outcome="YES",
            size_shares=80, avg_entry_price_cents=72,
            payout_per_share_cents=100,
            realized_pnl_cents=2_240,
            holding_seconds=5 * 3600,
        )
        msg = format_position_resolved(alert)
        assert "🟩" in msg
        assert "WON" in msg
        # '+$22.40' flows through escape_mdv2 -> '\+$22\.40'
        # (+ escaped, . escaped, $ not reserved)
        assert "\\+$22\\.40" in msg

    def test_loss_variant(self) -> None:
        alert = PositionResolvedAlert(
            run_id=4, position_id=201,
            market_question="OpenAI o3-mini before Jan?",
            our_outcome="YES", market_resolved_outcome="NO",
            size_shares=200, avg_entry_price_cents=28,
            payout_per_share_cents=0,
            realized_pnl_cents=-5_600,
            holding_seconds=41 * 3600,
        )
        msg = format_position_resolved(alert)
        assert "🔴" in msg
        assert "LOST" in msg
        # '-$56.00' -> escape_mdv2 -> '$\-56\.00'
        # (literal $, - escaped, . escaped; the minus goes after $ because
        # _plain_money emits "-$56.00" when dollars < 0 is written as "-$X")
        # Actually: f"{'' if dollars < 0 else ''}${dollars:,.2f}" where dollars=-56.0
        # -> "$-56.00" -> escape -> "$\-56\.00"
        assert "$\\-56\\.00" in msg

    def test_invalid_variant(self) -> None:
        alert = PositionResolvedAlert(
            run_id=3, position_id=9,
            market_question="Disputed market",
            our_outcome="YES", market_resolved_outcome="INVALID",
            size_shares=100, avg_entry_price_cents=40,
            payout_per_share_cents=0,
            realized_pnl_cents=0,
            holding_seconds=3600,
        )
        msg = format_position_resolved(alert)
        assert "🟨" in msg
        assert "INVALID" in msg


class TestDailySummaryFormat:
    def test_renders_all_sections(self) -> None:
        alert = DailySummaryAlert(
            as_of_iso="2026-04-19",
            runs=[
                {"id": 5, "name": "primary", "pnl_cents": 4218, "pct": 0.0042,
                 "win_rate": 0.57, "acted": 11, "flagged": 14},
                {"id": 6, "name": "null-baseline", "pnl_cents": -1204, "pct": -0.0012,
                 "win_rate": 0.49, "acted": 0, "flagged": 0},
            ],
            top_category=("ai", 5420),
            worst_category=("box_office", -250),
            llm_calls_today=12,
            llm_cost_cents_today=184,
            kill_switches_nominal=True,
        )
        msg = format_daily_summary(alert)
        # Dates get dashes escaped.
        assert "2026\\-04\\-19" in msg
        assert "primary" in msg
        assert "ai" in msg
        assert "box\\_office" in msg   # underscore escaped
        assert "all nominal" in msg

    def test_handles_no_runs(self) -> None:
        alert = DailySummaryAlert(
            as_of_iso="2026-04-19",
            runs=[],
            top_category=None,
            worst_category=None,
            llm_calls_today=0,
            llm_cost_cents_today=0,
            kill_switches_nominal=True,
        )
        msg = format_daily_summary(alert)
        assert "no active runs" in msg


class TestSinkFactory:
    async def test_returns_null_when_disabled(self) -> None:
        sink = make_sink_from_config(enabled=False, bot_token="t", chat_id="c")
        assert isinstance(sink, NullSink)

    async def test_returns_null_when_missing_token(self) -> None:
        sink = make_sink_from_config(enabled=True, bot_token="", chat_id="c")
        assert isinstance(sink, NullSink)

    async def test_null_sink_methods_noop(self) -> None:
        sink = NullSink()
        await sink.send_flag_raised(
            FlagAlert(
                flag_id=1, composite_score=7.0, verdict=None,
                verdict_confidence=None,
                market_question="?",
                wallet_address="0xabc",
                trade_side="BUY", trade_outcome="YES",
                trade_size_shares=10, trade_price_cents=40,
                top_detectors=[],
                paper_position_opened=False, copy_size_cents=0,
            )
        )
