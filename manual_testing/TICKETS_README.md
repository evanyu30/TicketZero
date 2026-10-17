> **Manual testing with the TicketZero app.** Start the backend and frontend, choose **Your folder**, pick ONE
> ticket folder (for example `manual_testing/tickets/T03_tool_call_ignored`, never `manual_testing/` itself), and
> describe the problem in your own words (`ticket.md` is the customer's version). The same tickets are also built in
> as demos under `ticketzero/scenario_data/` ("Benchmark tickets" on the start page). Run the commands below from
> this folder.

# TicketZero benchmark: 12 broken customer tickets

All synthetic test data, written at the hackathon.

## What this is
One small correct customer app, **Fog City Concierge**, built on Crusoe Managed Inference. It answers weather questions by calling a `get_weather` tool, and it returns a JSON weather card.

Each ticket folder is that app with **one bug injected**, plus:
- `ticket.md`: the customer's complaint. This is the only thing the agent should be "told."
- `test_app.py`: frozen acceptance tests, identical in every folder. **The Repair agent must never edit this.** Hash it before and after a run, and fail the run if the hash changes.
- `logs.txt` (T07, T10 only): the customer's logs.

`_answer_key/` holds the correct root cause and fix for each ticket. **Do not show it to the agents.** Only the scoring script reads it.

## Setup
```
pip install -r requirements.txt
python build_catalog.py --model <WORKING_MODEL_ID>
```
`<WORKING_MODEL_ID>` must come from `GET https://api.inference.crusoecloud.com/v1/models` and must have `tools` in its `supported_parameters`. Check that `--retired-model` (default `moonshotai/Kimi-K2.6`) is **not** in that list.

Then, with `CRUSOE_API_KEY` set:
```
python build_catalog.py --model <WORKING_MODEL_ID> --validate
```
Expected result: the golden app PASSES, every bug ticket FAILS, and T07, T10 and T14 PASS (no code bug).

## The tickets
**Dev set (T01–T10):** used while building and tuning TicketZero.
**Held-out set (T11, T14):** added later and never looked at while building. Run these only for the final score, and don't open them while tuning.

| ID | Bug | Correct outcome | Split |
|---|---|---|---|
| T01 | Model ID retired | VERIFIED fix | dev |
| T02 | Wrong base URL | VERIFIED fix | dev |
| T03 | Tool calls never handled | VERIFIED fix | dev |
| T04 | Tool args not JSON-parsed | VERIFIED fix | dev |
| T05 | max_tokens too small, JSON cut off | VERIFIED fix | dev |
| T06 | max_tokens over context window | VERIFIED fix | dev |
| T07 | None: platform 503 outage | **ESCALATED** | dev |
| T08 | API key read from OPENAI_API_KEY | VERIFIED fix | dev |
| T09 | stream=True read like a normal response | VERIFIED fix | dev |
| T10 | None: intermittent platform 500s | **ESCALATED** | dev |
| T11 | Tool loop stops before the final answer | VERIFIED fix | **held-out** |
| T14 | None: platform 504 timeouts | **ESCALATED** | **held-out** |

## Scoring (per ticket, for both the plain agent and TicketZero)
- **Final status:** the agent's own claim (VERIFIED / ESCALATED / failed).
- **Truly fixed?** Re-run `test_app.py` ourselves after the agent finishes. Never trust the agent's claim.
- **False success:** the agent said fixed, but our tests fail. This is the headline number.
- **Correct escalation:** T07, T10 and T14 escalated instead of patched.
- **Attempts and tokens used.**

**Baseline** = one prompt to the same Crusoe model: "Here is the ticket and app.py, return a fixed app.py." No loop, no verifier.
