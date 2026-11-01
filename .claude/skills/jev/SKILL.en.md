---
name: jev
description: Call TypeSafe's Jev decision model through OpenRouter to get probability-weighted verdicts on structured questions - yes/no, single choice, or ordered scores, several per request. About 1-2 seconds and about USD 0.00002 per call. Suited to classification, routing, and first-pass screening; do not use it for questions that need reasoning, calculation, or fact-checking.
---

# jev

Jev is a model that only decides: give it the current state and questions with descriptions, and it returns choices with probabilities, without generating text. Developer notes are in `docs/jev.md`.

## When to use it

- Classification and routing: categorizing feedback, assigning tasks, deciding whether a human must step in.
- Yes/no judgments, one-of-many choices, and ordered levels such as "urgency".
- First-pass screening that is later checked by a person or by full reasoning.

Do not use it for: questions that need reasoning, calculation, multi-step judgment, or fact-checking; decisions whose result directly affects the outside world; open questions without fixed options.

## Data boundary

`state` and the questions are sent to OpenRouter and TypeSafe. Send only the minimum needed for the decision; never send personal information, credentials, or compliance-restricted data, and do not call it if you cannot avoid that. A request over 40,000 characters fails outright without truncation; extract the relevant part first.

## Usage

A single-choice question can use the argument shorthand:

```bash
python3 .claude/skills/jev/scripts/jev_decide.py --question "Which category is this feedback? Feedback: Chinese text turns into boxes when exporting PDF." \
  --option bug --option suggestion --option question [--state-file material.txt]
```

The full form passes JSON on standard input and can ask several questions in one request:

```bash
printf '%s' '{
  "state": {"ticket": "Chinese text turns into boxes when exporting PDF"},
  "questions": {
    "is_bug":  {"type": "noul",   "instructions": "Is this a software defect?",
                "criteria": {"true": "Describes abnormal behavior", "false": "A question or suggestion"}},
    "team":    {"type": "choice", "instructions": "Which team owns it?",
                "criteria": {"export": "Export and printing", "editor": "Editor", "account": "Accounts"}},
    "urgency": {"type": "score",  "instructions": "How urgent?",
                "criteria": ["Fix in the next release", "Fix this week", "Fix now"]}
  }
}' | python3 .claude/skills/jev/scripts/jev_decide.py --json -
```

- `state`: an object or text holding the basis for the decision.
- `criteria` for `noul` may contain only `true` and `false`; `choice` needs at least two "option: description" pairs; `score` needs at least two levels in order.
- `--dry-run`: validate and print the request body only; no network access and no credentials needed.

## Reading results

- `noul`: the probability of "yes", 0-1. Close to 0.5 means uncertain, not "moderate".
- `choice`: `choice` is the selected option, `probabilities` gives each option's probability, and `confidence` shows how concentrated the probabilities are.
- `score`: `score` is the probability-weighted position, where 0 is the first level; `legend` lists the levels.
- `usage.cost`: the cost of this call in US dollars, charged on input only.

The probabilities are not calibrated to your data, and Jev often returns 1 for clear questions. For automatic routing, set thresholds on samples with known answers first, based on the cost of errors rather than round numbers chosen by feel; pass anything below the threshold to full reasoning or a human.

## Failures

Exit code 1 with `{"error": …}` output: incomplete input, missing credentials, OpenRouter unreachable, or the server rejected the request. Only the error category or HTTP status is printed, never server text that might echo credentials. Credentials are read from the `OPENROUTER_API_KEY` environment variable or Pi's openrouter login; the network proxy follows `httpProxy` in Pi's settings. It never switches to another model automatically.

All HTTP redirects are refused so credentials are not forwarded elsewhere. Responses must pass structural checks on question type, options, probabilities, and usage; otherwise an error is reported and no successful verdict is returned.

Usage for each successful call is recorded in the Desk usage store (category `jev`) as numbers only; a recording failure produces an explicit warning without affecting the verdict already returned.
