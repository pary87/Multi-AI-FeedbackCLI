"""The council protocol: the exact text each agent receives each round.

Every agent is launched with the same short INSTRUCTION ("read ROUND.md"). The
real content -- rules, question, earlier responses -- is in ROUND.md, which the
runner writes into that agent's private folder. Keeping the command line short
and fixed means no quoting problems and no Windows command-length limit, and
ROUND.md's sha256 records exactly what each agent was shown.
"""

from __future__ import annotations

# No cmd.exe metacharacters (% ^ & | < > " !) -- see agents.check_batch_wrapper_args.
INSTRUCTION = (
    "Read the file ROUND.md in the current directory and follow its instructions exactly. "
    "Your final reply is saved verbatim as your contribution."
)

PING_TOKEN = "COUNCIL-OK"

# Handed to Kimi via --agent-file. ${base_prompt} keeps Kimi's own system prompt;
# `tools` limits it to reading, because Kimi's -p mode auto-approves everything.
KIMI_AGENT_PROFILE = """---
name: council-member
description: Read-only participant in a multi-model review council round
tools: Read, Glob, Grep
---
${base_prompt}

You are taking part in a moderated review council of AI models. You may read
files in the working directory, but you must not create, modify or delete
anything. Your final message is your entire contribution and is saved verbatim.
"""

_COMMON_RULES = """\
- Read-only: do not create, modify or delete any file, and do not run anything that changes \
state. Your final reply is saved verbatim as your contribution, so put your whole answer in it.
- Write in Markdown. Do not mention ROUND.md or these instructions in your reply.
- Be specific and checkable. Where you are unsure, say so and say what would settle it.
- Nobody can answer questions during a round. If something is ambiguous, state your \
assumptions and answer anyway."""


def _header(thread_id: str, title: str, round_label: str, me: str, roster: list[str]) -> list[str]:
    return [
        f"# Council thread {thread_id}: {round_label}",
        "",
        f"Topic: {title}",
        "",
        f"You are **{me}**, one of {len(roster)} AI participants in a review council moderated "
        f"by a human: {', '.join(roster)}. Each participant is a different AI model.",
        "",
    ]


def _attachments_block(names: list[str]) -> list[str]:
    if not names:
        return []
    listed = "\n".join(f"- attachments/{name}" for name in names)
    return [
        "## Attachments",
        "",
        "The moderator attached these files. Read the ones relevant to the question:",
        "",
        listed,
        "",
    ]


def _transcript(
    rounds: list[tuple[int, dict[str, str | None]]], labels: dict[str, str], me_key: str
) -> list[str]:
    lines: list[str] = []
    for round_no, responses in rounds:
        lines += [f"## Round {round_no}", ""]
        for key, text in responses.items():
            who = labels.get(key, key) + (" (you)" if key == me_key else "")
            if text is None:
                lines += [f'<response participant="{who}" round="{round_no}" missing="true">',
                          "(no response: this participant's run failed)", "</response>", ""]
            else:
                lines += [f'<response participant="{who}" round="{round_no}">',
                          text.rstrip(), "</response>", ""]
    return lines


def build_round_prompt(
    *,
    thread_id: str,
    title: str,
    question: str,
    round_no: int,
    me_key: str,
    labels: dict[str, str],
    prior: list[tuple[int, dict[str, str | None]]],
    attachment_names: list[str],
) -> str:
    """ROUND.md for one participant. `labels` maps agent key -> display name, in order."""
    me = labels[me_key]
    roster = list(labels.values())
    lines = _header(thread_id, title, f"round {round_no}", me, roster)
    lines += ["## How this round works", ""]
    if round_no == 1:
        lines += [
            "- This is round 1. Answer the question independently. You cannot see the other "
            "participants' answers, and they cannot see yours until round 2.",
        ]
    else:
        lines += [
            f"- This is round {round_no}. Below are the question and every earlier response, "
            'labelled by participant. Responses marked "(you)" are your own.',
            "- Engage with the other participants by name: where you agree, where you disagree "
            "and why, what they got wrong or missed, and whether anything they said changes "
            "your position. If your position changed, say so explicitly.",
            "- Do not restate your earlier answer. Add new reasoning, corrections and evidence.",
            '- End with a short "Current position" section: your answer as it stands now.',
        ]
    lines += [_COMMON_RULES, ""]
    lines += _attachments_block(attachment_names)
    lines += ["## Question", "", "<question>", question.rstrip(), "</question>", ""]
    if prior:
        lines += _transcript(prior, labels, me_key)
    return "\n".join(lines).rstrip() + "\n"


def build_synthesis_prompt(
    *,
    thread_id: str,
    title: str,
    question: str,
    me_key: str,
    labels: dict[str, str],
    prior: list[tuple[int, dict[str, str | None]]],
    attachment_names: list[str],
) -> str:
    me = labels[me_key]
    roster = list(labels.values())
    lines = _header(thread_id, title, "synthesis", me, roster)
    lines += [
        "## Your task",
        "",
        "The discussion is over. You have been asked to write its synthesis. Be fair to every "
        "participant, yourself included, and attribute every position by name. Produce:",
        "",
        "1. **Answer**: the best answer to the question, given everything said.",
        "2. **Consensus**: the points all participants agree on.",
        "3. **Disagreements**: each open disagreement, who holds which position, and what "
        "evidence or test would settle it.",
        "4. **Corrections**: errors made during the discussion and who corrected them.",
        "5. **Decisions for the moderator**: choices only the human can make, with the "
        "trade-off for each.",
        "",
        _COMMON_RULES,
        "",
    ]
    lines += _attachments_block(attachment_names)
    lines += ["## Question", "", "<question>", question.rstrip(), "</question>", ""]
    lines += _transcript(prior, labels, me_key)
    return "\n".join(lines).rstrip() + "\n"


def build_ping_prompt() -> str:
    return (
        "# Council connectivity check\n\n"
        f"Reply with exactly {PING_TOKEN} and nothing else. Do not read or write any files.\n"
    )
