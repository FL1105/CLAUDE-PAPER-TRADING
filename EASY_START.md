# Easy start: the simple learning paper bot

**Paper only.** It watches real Polymarket BTC 5-minute Up/Down markets and pretends to trade.
It never places an order, never needs a wallet, a key or an account.

## Run it
- **Windows:** double-click `start_bot.bat` (first run installs everything).
- **Mac/Linux:** `./start_bot.sh`

Leave the window open. Stop with Ctrl+C. Run it again any time: it resumes where it left off.
Check progress without running: `polybot learn status` (inside the virtual environment).

## What "learns" means here
Every 5 minutes one market resolves. Six simple rules (always Up, always Down, favourite,
underdog, follow the last minute's move, fade it) each decide at two moments (240 s and 120 s
before the end) and "trade" at the REAL ask, after a 2-second delay, paying the documented fee.
After each window the bot updates every rule's average return and its uncertainty. Its own pick
each window comes from Thompson sampling plus a "sit out" option worth zero, using only past
windows. Early on it sits out most of the time, which is correct.

A rule is only marked **PROVEN** when, after a correction for testing 12 rules at once, the lower
confidence bound of its net return is above zero AND it has 100+ windows. On simulated data with
no edge this made 0 false proofs in 60 runs; with a planted edge it found it.

## How to read the table
`lower bd` is the pessimistic estimate. Ignore `net ret` and the paper bankroll until a rule is
PROVEN: a few windows of luck look like skill. Expect to need a few days (about 288 windows a
day) before any rule could be proven, and expect that none ever is: earlier real-data tests found
no edge in this market, and the learner is built to say so.

## Limits
- It learns which of six simple rules is best; it cannot invent an edge that is not in the data.
- A "proven" rule is a lead to keep testing on paper, not a reason to trade real money.
