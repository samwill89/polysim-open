"""InvestigatorContext.external_signal_note renders into the prompt
(and only when present) — the §5.3 no-network guard stays intact because
the pipeline pre-fetches the note."""

from __future__ import annotations

from polysim.agents.investigator import InvestigatorContext, _build_user_prompt


def _ctx(note: str | None) -> InvestigatorContext:
    return InvestigatorContext(
        market_id="m1",
        market_question="Will X?",
        market_category="box_office",
        current_bid_cents=40,
        current_ask_cents=42,
        cohort_side="YES",
        cohort_wallets=["0xabc"],
        cohort_signal_strength=0.8,
        external_signal_note=note,
    )


def test_prompt_includes_note_when_present() -> None:
    prompt = _build_user_prompt(_ctx("attention z=+2.1, velocity 3.0x"))
    assert "PUBLIC CONVERSATION" in prompt
    assert "z=+2.1" in prompt


def test_prompt_omits_section_when_absent() -> None:
    prompt = _build_user_prompt(_ctx(None))
    assert "PUBLIC CONVERSATION" not in prompt
